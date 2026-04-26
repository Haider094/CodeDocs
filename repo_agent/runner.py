import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from colorama import Fore, Style
from tqdm import tqdm

from repo_agent.change_detector import ChangeDetector
from repo_agent.chat_engine import ChatEngine
from repo_agent.doc_meta_info import DocItem, DocItemStatus, MetaInfo, need_to_generate
from repo_agent.file_handler import FileHandler
from repo_agent.log import logger
from repo_agent.multi_task_dispatch import worker
from repo_agent.project_manager import ProjectManager
from repo_agent.settings import SettingsManager
from repo_agent.utils.meta_info_utils import delete_fake_files, make_fake_files


class Runner:
    def __init__(self):
        self.setting = SettingsManager.get_setting()
        self.absolute_project_hierarchy_path = (
            self.setting.project.target_repo / self.setting.project.hierarchy_name
        )

        self.project_manager = ProjectManager(
            repo_path=self.setting.project.target_repo,
            project_hierarchy=self.setting.project.hierarchy_name,
        )
        self.change_detector = ChangeDetector(
            repo_path=self.setting.project.target_repo
        )
        self.chat_engine = ChatEngine(project_manager=self.project_manager)

        if not self.absolute_project_hierarchy_path.exists():
            file_path_reflections, jump_files = make_fake_files()
            self.meta_info = MetaInfo.init_meta_info(file_path_reflections, jump_files)
            self.meta_info.checkpoint(
                target_dir_path=self.absolute_project_hierarchy_path
            )
        else:  # Load from existing .project_hierarchy folder if it exists
            self.meta_info = MetaInfo.from_checkpoint_path(
                self.absolute_project_hierarchy_path
            )

        self.meta_info.checkpoint(  # Re-write global info to .project_doc_record after whitelist update
            target_dir_path=self.absolute_project_hierarchy_path
        )
        self.runner_lock = threading.Lock()

    def get_all_pys(self, directory):
        """
        Get all Python files in the given directory.

        Args:
            directory (str): The directory to search.

        Returns:
            list: A list of paths to all Python files.
        """
        python_files = []

        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    python_files.append(os.path.join(root, file))

        return python_files

    def generate_doc_for_a_single_item(self, doc_item: DocItem):
        """Generate documentation for a single code object."""
        try:
            if not need_to_generate(doc_item, self.setting.project.ignore_list):
                print(
                    f"Content ignored/Document generated, skipping: {doc_item.get_full_name()}"
                )
            else:
                print(
                    f" -- Generating document  {Fore.LIGHTYELLOW_EX}{doc_item.item_type.name}: {doc_item.get_full_name()}{Style.RESET_ALL}"
                )
                response_message = self.chat_engine.generate_doc(
                    doc_item=doc_item,
                )
                doc_item.md_content.append(response_message)  # type: ignore
                doc_item.item_status = DocItemStatus.doc_up_to_date
                self.meta_info.checkpoint(
                    target_dir_path=self.absolute_project_hierarchy_path
                )
        except Exception:
            logger.exception(
                f"Document generation failed after multiple attempts, skipping: {doc_item.get_full_name()}"
            )
            doc_item.item_status = DocItemStatus.doc_has_not_been_generated

    def first_generate(self):
        """Generate all documents and refresh/save documentation info to the file system when done."""
        logger.info("Starting to generate documentation")
        check_task_available_func = partial(
            need_to_generate, ignore_list=self.setting.project.ignore_list
        )
        task_manager = self.meta_info.get_topology(check_task_available_func)
        before_task_len = len(task_manager.task_dict)

        if not self.meta_info.in_generation_process:
            self.meta_info.in_generation_process = True
            logger.info("Init a new task-list")
        else:
            logger.info("Load from an existing task-list")
        self.meta_info.print_task_list(task_manager.task_dict)

        try:
            # Create and start worker threads
            threads = [
                threading.Thread(
                    target=worker,
                    args=(
                        task_manager,
                        process_id,
                        self.generate_doc_for_a_single_item,
                    ),
                )
                for process_id in range(self.setting.project.max_thread_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # Refresh documents after all tasks complete
            self.markdown_refresh()

            # Update document version
            self.meta_info.document_version = (
                self.change_detector.repo.head.commit.hexsha
            )
            self.meta_info.in_generation_process = False
            self.meta_info.checkpoint(
                target_dir_path=self.absolute_project_hierarchy_path
            )
            logger.info(
                f"Successfully generated {before_task_len - len(task_manager.task_dict)} documents."
            )

        except BaseException as e:
            logger.error(
                f"An error occurred: {e}. {before_task_len - len(task_manager.task_dict)} docs are generated at this time"
            )

    def markdown_refresh(self):
        """Refresh the latest documentation info into the markdown folder."""
        with self.runner_lock:
            # Define markdown folder path
            markdown_folder = (
                Path(self.setting.project.target_repo)
                / self.setting.project.markdown_docs_name
            )

            # Delete and recreate the directory
            if markdown_folder.exists():
                logger.debug(f"Deleting existing contents of {markdown_folder}")
                shutil.rmtree(markdown_folder)
            markdown_folder.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Created markdown folder at {markdown_folder}")

        # Iterate over file list and generate markdown
        file_item_list = self.meta_info.get_all_files()
        logger.debug(f"Found {len(file_item_list)} files to process.")

        for file_item in tqdm(file_item_list):
            # Check for documentation content
            def recursive_check(doc_item) -> bool:
                if doc_item.md_content:
                    return True
                for child in doc_item.children.values():
                    if recursive_check(child):
                        return True
                return False

            if not recursive_check(file_item):
                logger.debug(
                    f"No documentation content for: {file_item.get_full_name()}, skipping."
                )
                continue

            # Generate markdown content
            markdown = ""
            for child in file_item.children.values():
                markdown += self.to_markdown(child, 2)

            if not markdown:
                logger.warning(
                    f"No markdown content generated for: {file_item.get_full_name()}"
                )
                continue

            # Determine and create file path
            file_path = Path(
                self.setting.project.markdown_docs_name
            ) / file_item.get_file_name().replace(".py", ".md")
            abs_file_path = self.setting.project.target_repo / file_path
            logger.debug(f"Writing markdown to: {abs_file_path}")

            # Ensure directory exists
            abs_file_path.parent.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory exists: {abs_file_path.parent}")

            # Use lock to protect file write operations
            with self.runner_lock:
                for attempt in range(3):  # Retry up to 3 times
                    try:
                        with open(abs_file_path, "w", encoding="utf-8") as file:
                            file.write(markdown)
                        logger.debug(f"Successfully wrote to {abs_file_path}")
                        break
                    except IOError as e:
                        logger.error(
                            f"Failed to write {abs_file_path} on attempt {attempt + 1}: {e}"
                        )
                        time.sleep(1)  # Wait before retrying

        logger.info(
            f"Markdown documents have been refreshed at {self.setting.project.markdown_docs_name}"
        )

    def to_markdown(self, item, now_level: int) -> str:
        """Convert a code item to markdown-formatted text."""
        markdown_content = (
            "#" * now_level + f" {item.item_type.to_str()} {item.obj_name}"
        )
        if "params" in item.content.keys() and item.content["params"]:
            markdown_content += f"({', '.join(item.content['params'])})"
        markdown_content += "\n"
        if item.md_content:
            markdown_content += f"{item.md_content[-1]}\n"
        else:
            markdown_content += "Doc is waiting to be generated...\n"
        for child in item.children.values():
            markdown_content += self.to_markdown(child, now_level + 1)
            markdown_content += "***\n"
        return markdown_content

    def git_commit(self, commit_message):
        try:
            subprocess.check_call(
                ["git", "commit", "--no-verify", "-m", commit_message],
                shell=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while trying to commit {str(e)}")

    def run(self):
        """
        Runs the document update process.

        This method detects the changed Python files, processes each file, and updates the documents accordingly.

        Returns:
            None
        """

        if self.meta_info.document_version == "":
            # Auto-detect via document_version whether this is the first generation run
            self.first_generate()  # Run first_generate if this is the first documentation task
            self.meta_info.checkpoint(
                target_dir_path=self.absolute_project_hierarchy_path,
                flash_reference_relation=True,
            )  # Write post-generation meta info (including reference relations) to .project_doc_record
            return

        if (
            not self.meta_info.in_generation_process
        ):  # Start detecting changes if not already in generation process
            logger.info("Starting to detect changes.")

            """New approach:
            1. Create a new project-hierarchy
            2. Merge with old hierarchy, handling the following cases:
            - New file created: generate the corresponding doc
            - File/object deleted: remove the corresponding doc (rename counts as delete + add)
            - Reference relationships changed: regenerate the affected obj-doc

            After merge, in new_meta_info:
            1. New files have no docs, so after metainfo merge they still have no docs
            2. Deleted files and objects are not in the new meta, so docs are automatically removed
            3. Only need to watch modified files and files whose references need to be notified"""
            file_path_reflections, jump_files = make_fake_files()
            new_meta_info = MetaInfo.init_meta_info(file_path_reflections, jump_files)
            new_meta_info.load_doc_from_older_meta(self.meta_info)

            self.meta_info = new_meta_info  # Update self.meta_info to the new version
            self.meta_info.in_generation_process = True  # Mark as in-progress after detecting changes

        # Process the task queue
        check_task_available_func = partial(
            need_to_generate, ignore_list=self.setting.project.ignore_list
        )

        task_manager = self.meta_info.get_task_manager(
            self.meta_info.target_repo_hierarchical_tree,
            task_available_func=check_task_available_func,
        )

        for item_name, item_type in self.meta_info.deleted_items_from_older_meta:
            print(
                f"{Fore.LIGHTMAGENTA_EX}[Dir/File/Obj Delete Dected]: {Style.RESET_ALL} {item_type} {item_name}"
            )
        self.meta_info.print_task_list(task_manager.task_dict)
        if task_manager.all_success:
            logger.info(
                "No tasks in the queue, all documents are completed and up to date."
            )

        threads = [
            threading.Thread(
                target=worker,
                args=(task_manager, process_id, self.generate_doc_for_a_single_item),
            )
            for process_id in range(self.setting.project.max_thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.meta_info.in_generation_process = False
        self.meta_info.document_version = self.change_detector.repo.head.commit.hexsha

        self.meta_info.checkpoint(
            target_dir_path=self.absolute_project_hierarchy_path,
            flash_reference_relation=True,
        )
        logger.info(f"Doc has been forwarded to the latest version")

        self.markdown_refresh()
        delete_fake_files()

        logger.info(f"Starting to git-add DocMetaInfo and newly generated Docs")
        time.sleep(1)

        # Add updated (unstaged) Markdown files from this run to the staging area
        git_add_result = self.change_detector.add_unstaged_files()

        if len(git_add_result) > 0:
            logger.info(
                f"Added {[file for file in git_add_result]} to the staging area."
            )

        # self.git_commit(f"Update documentation for {file_handler.file_path}") # commit changes

    def add_new_item(self, file_handler, json_data):
        """
        Add new projects to the JSON file and generate corresponding documentation.

        Args:
            file_handler (FileHandler): The file handler object for reading and writing files.
            json_data (dict): The JSON data storing the project structure information.

        Returns:
            None
        """
        file_dict = {}
        # All objects in a newly added file need documentation
        for (
            structure_type,
            name,
            start_line,
            end_line,
            parent,
            params,
        ) in file_handler.get_functions_and_classes(file_handler.read_file()):
            code_info = file_handler.get_obj_code_info(
                structure_type, name, start_line, end_line, parent, params
            )
            response_message = self.chat_engine.generate_doc(code_info, file_handler)
            md_content = response_message.content
            code_info["md_content"] = md_content
            # Add a new object to the file_dict
            file_dict[name] = code_info

        json_data[file_handler.file_path] = file_dict
        # Write the new entry to the json file
        with open(self.project_manager.project_hierarchy, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=4, ensure_ascii=False)
        logger.info(
            f"The structural information of the newly added file {file_handler.file_path} has been written into a JSON file."
        )
        # Convert the changed json content to markdown
        markdown = file_handler.convert_to_markdown_file(
            file_path=file_handler.file_path
        )
        # Write markdown content to the .md file
        file_handler.write_file(
            os.path.join(
                self.project_manager.repo_path,
                self.setting.project.markdown_docs_name,
                file_handler.file_path.replace(".py", ".md"),
            ),
            markdown,
        )
        logger.info(f"Generated Markdown documentation for new file {file_handler.file_path}.")

    def process_file_changes(self, repo_path, file_path, is_new_file):
        """
        This function is called in the loop of detected changed files. Its purpose is to process changed files according to the absolute file path, including new files and existing files.
        Among them, changes_in_pyfile is a dictionary that contains information about the changed structures. An example format is: {'added': {'add_context_stack', '__init__'}, 'removed': set()}

        Args:
            repo_path (str): The path to the repository.
            file_path (str): The relative path to the file.
            is_new_file (bool): Indicates whether the file is new or not.

        Returns:
            None
        """

        file_handler = FileHandler(
            repo_path=repo_path, file_path=file_path
        )  # Handler for the changed file
        # Get the entire py file source code
        source_code = file_handler.read_file()
        changed_lines = self.change_detector.parse_diffs(
            self.change_detector.get_file_diff(file_path, is_new_file)
        )
        changes_in_pyfile = self.change_detector.identify_changes_in_structure(
            changed_lines, file_handler.get_functions_and_classes(source_code)
        )
        logger.info(f"Detected changed objects:\n{changes_in_pyfile}")

        # Check if the corresponding .py file path exists in project_hierarchy.json
        with open(self.project_manager.project_hierarchy, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        # If the file is found in the hierarchy
        if file_handler.file_path in json_data:
            # Update the json file content
            json_data[file_handler.file_path] = self.update_existing_item(
                json_data[file_handler.file_path], file_handler, changes_in_pyfile
            )
            # Write the updated file back to json
            with open(
                self.project_manager.project_hierarchy, "w", encoding="utf-8"
            ) as f:
                json.dump(json_data, f, indent=4, ensure_ascii=False)

            logger.info(f"Updated JSON structure info for {file_handler.file_path}.")

            # Convert the changed json content to markdown
            markdown = file_handler.convert_to_markdown_file(
                file_path=file_handler.file_path
            )
            # Write markdown content to the .md file
            file_handler.write_file(
                os.path.join(
                    self.setting.project.markdown_docs_name,
                    file_handler.file_path.replace(".py", ".md"),
                ),
                markdown,
            )
            logger.info(f"Updated Markdown documentation for {file_handler.file_path}.")

        # If the file was not found, add it as a new entry
        else:
            self.add_new_item(file_handler, json_data)

        # Add updated (unstaged) Markdown files to the staging area
        git_add_result = self.change_detector.add_unstaged_files()

        if len(git_add_result) > 0:
            logger.info(f"Added {[file for file in git_add_result]} to the staging area.")

        # self.git_commit(f"Update documentation for {file_handler.file_path}") # commit changes

    def update_existing_item(self, file_dict, file_handler, changes_in_pyfile):
        """
        Update existing projects.

        Args:
            file_dict (dict): A dictionary containing file structure information.
            file_handler (FileHandler): The file handler object.
            changes_in_pyfile (dict): A dictionary containing information about the objects that have changed in the file.

        Returns:
            dict: The updated file structure information dictionary.
        """
        new_obj, del_obj = self.get_new_objects(file_handler)

        # Handle deleted objects
        for obj_name in del_obj:  # Objects that were truly deleted
            if obj_name in file_dict:
                del file_dict[obj_name]
                logger.info(f"Deleted object {obj_name}.")

        referencer_list = []

        # Generate file structure info and get all current objects (post-update structure)
        current_objects = file_handler.generate_file_structure(file_handler.file_path)

        current_info_dict = {obj["name"]: obj for obj in current_objects.values()}

        # Update global file structure info (e.g. code start/end lines)
        for current_obj_name, current_obj_info in current_info_dict.items():
            if current_obj_name in file_dict:
                # If the current object exists in the old list, update its info
                file_dict[current_obj_name]["type"] = current_obj_info["type"]
                file_dict[current_obj_name]["code_start_line"] = current_obj_info[
                    "code_start_line"
                ]
                file_dict[current_obj_name]["code_end_line"] = current_obj_info[
                    "code_end_line"
                ]
                file_dict[current_obj_name]["parent"] = current_obj_info["parent"]
                file_dict[current_obj_name]["name_column"] = current_obj_info[
                    "name_column"
                ]
            else:
                # If the current object doesn't exist in the old list, add it
                file_dict[current_obj_name] = current_obj_info

        # For each object: get its list of referencers
        for obj_name, _ in changes_in_pyfile["added"]:
            for current_object in current_objects.values():  # Use new_objects to get parameters needed for find_all_referencer
                if (
                    obj_name == current_object["name"]
                ):  # Only add referencers when the added object name matches
                    referencer_obj = {
                        "obj_name": obj_name,
                        "obj_referencer_list": self.project_manager.find_all_referencer(
                            variable_name=current_object["name"],
                            file_path=file_handler.file_path,
                            line_number=current_object["code_start_line"],
                            column_number=current_object["name_column"],
                        ),
                    }
                    referencer_list.append(referencer_obj)

        with ThreadPoolExecutor(max_workers=5) as executor:
            # Execute concurrently via thread pool
            futures = []
            for changed_obj in changes_in_pyfile["added"]:  # For each object to process
                for ref_obj in referencer_list:
                    if (
                        changed_obj[0] == ref_obj["obj_name"]
                    ):  # Find its referencer dict in referencer_list
                        future = executor.submit(
                            self.update_object,
                            file_dict,
                            file_handler,
                            changed_obj[0],
                            ref_obj["obj_referencer_list"],
                        )
                        print(
                            f"Generating doc for {Fore.CYAN}{changed_obj[0]}{Style.RESET_ALL} in {Fore.CYAN}{file_handler.file_path}{Style.RESET_ALL}."
                        )
                        futures.append(future)

            for future in futures:
                future.result()

        # Return the updated file dict
        return file_dict

    def update_object(self, file_dict, file_handler, obj_name, obj_referencer_list):
        """
        Generate documentation content and update corresponding field information of the object.

        Args:
            file_dict (dict): A dictionary containing old object information.
            file_handler: The file handler.
            obj_name (str): The object name.
            obj_referencer_list (list): The list of object referencers.

        Returns:
            None
        """
        if obj_name in file_dict:
            obj = file_dict[obj_name]
            response_message = self.chat_engine.generate_doc(
                obj, file_handler, obj_referencer_list
            )
            obj["md_content"] = response_message.content

    def get_new_objects(self, file_handler):
        """
        The function gets the added and deleted objects by comparing the current version and the previous version of the .py file.

        Args:
            file_handler (FileHandler): The file handler object.

        Returns:
            tuple: A tuple containing the added and deleted objects, in the format (new_obj, del_obj)

        Output example:
            new_obj: ['add_context_stack', '__init__']
            del_obj: []
        """
        current_version, previous_version = file_handler.get_modified_file_versions()
        parse_current_py = file_handler.get_functions_and_classes(current_version)
        parse_previous_py = (
            file_handler.get_functions_and_classes(previous_version)
            if previous_version
            else []
        )

        current_obj = {f[1] for f in parse_current_py}
        previous_obj = {f[1] for f in parse_previous_py}

        new_obj = list(current_obj - previous_obj)
        del_obj = list(previous_obj - current_obj)
        return new_obj, del_obj


if __name__ == "__main__":
    runner = Runner()

    runner.run()

    logger.info("Documentation task completed.")
