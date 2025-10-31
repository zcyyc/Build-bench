import ast
import os
from typing import List, Generator, Union
from tree_sitter import Language, Parser, Node
import tree_sitter_cpp as tscpp
import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_typescript as tsts
import tree_sitter_rust as tsrust
import xml.etree.ElementTree as ET


def parse_python_file(file_path, file_content=None):
    """Parse a Python file to extract class and function definitions with their line numbers.
    :param file_path: Path to the Python file.
    :return: Class names, function names, and file contents
    """
    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                parsed_data = ast.parse(file_content)
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""
    else:
        try:
            parsed_data = ast.parse(file_content)
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""

    class_info = []
    function_names = []
    class_methods = set()

    for node in ast.walk(parsed_data):
        if isinstance(node, ast.ClassDef):
            methods = []
            for n in node.body:
                if isinstance(n, ast.FunctionDef):
                    methods.append(
                        {
                            "name": n.name,
                            "start_line": n.lineno,
                            "end_line": n.end_lineno,
                            "text": file_content.splitlines()[
                                n.lineno - 1 : n.end_lineno
                            ],
                        }
                    )
                    class_methods.add(n.name)
            class_info.append(
                {
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "text": file_content.splitlines()[
                        node.lineno - 1 : node.end_lineno
                    ],
                    "methods": methods,
                }
            )
        elif isinstance(node, ast.FunctionDef) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
            if node.name not in class_methods:
                function_names.append(
                    {
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": node.end_lineno,
                        "text": file_content.splitlines()[
                            node.lineno - 1 : node.end_lineno
                        ],
                    }
                )

    return class_info, function_names, file_content.splitlines()


def traverse(node: Node) -> Generator[Node, None, None]:
    cursor = node.walk()
    visited_children = False
    while True:
        if not visited_children:
            yield cursor.node
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break


def get_child(node: Node, type_name: str, skip: int = 0) -> Union[Node, None]:
    for child in node.children:
        if child.type == type_name:
            if skip == 0:
                return child
            skip = skip - 1
    return None


def get_child_chain(node: Node, type_names: List[str]) -> Union[str, None]:
    for type_name in type_names:
        node = get_child(node, type_name)
        if node is None:
            return node
    return node


def get_name(node: Node, type_name: str = "identifier") -> Union[str, None]:
    return get_child(node, type_name).text.decode("utf-8")


def parse_java_file(file_path, file_content=None):
    """Parse a Java file to extract interface definitions and class definitions with their line numbers.
    :param file_path: Path to the Java file.
    :return: Class names, and file contents
    """
    parser = Parser(Language(tsjava.language()))

    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], ""
    else:
        try:
            tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], ""

    class_info = []

    for node in traverse(tree.root_node):
        if node.type == "interface_declaration" or node.type == "class_declaration":
            info = None
            if node.type == "interface_declaration":
                info = class_info
            elif node.type == "class_declaration":
                info = class_info

            methods = []
            for n in traverse(node):
                if n.type == "method_declaration":
                    methods.append(
                        {
                            "name": get_name(n),
                            "start_line": n.start_point.row,
                            "end_line": n.end_point.row,
                            "text": n.text.decode("utf-8").splitlines(),
                        }
                    )
            info.append(
                {
                    "name": get_name(node),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                    "methods": methods,
                }
            )

    return class_info, file_content.splitlines()


def parse_go_file(file_path, file_content=None):
    """Parse a Go file to extract class and function definitions with their line numbers.
    :param file_path: Path to the Python file.
    :return: Class names, function names, and file contents
    """
    parser = Parser(Language(tsgo.language()))

    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""
    else:
        try:
            tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""

    class_info = []
    function_names = []

    for node in traverse(tree.root_node):
        if node.type == "type_declaration":
            type_spec = get_child(node, "type_spec")
            if type_spec is None:
                continue
            name = get_name(type_spec, "type_identifier")
            methods = []
            class_info.append(
                {
                    "name": name,
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                    "methods": methods,
                }
            )
        elif node.type == "method_declaration":
            function_names.append(
                {
                    "name": get_name(node, "field_identifier"),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )
        elif node.type == "function_declaration":
            function_names.append(
                {
                    "name": get_name(node, "identifier"),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )

    return class_info, function_names, file_content.splitlines()


def parse_rust_file(file_path, file_content=None):
    """Parse a Rust file to extract class and function definitions with their line numbers.
    :param file_path: Path to the Python file.
    :return: Class names, function names, and file contents
    """
    parser = Parser(Language(tsrust.language()))

    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""
    else:
        try:
            tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""

    class_info = []
    function_names = []
    class_to_methods = {}

    def get_type(node: Node):
        if node.type == "type_identifier":
            return node.text.decode("utf-8")
        elif node.type == "generic_type":
            return get_type(node.child_by_field_name("type"))
        return None

    for node in traverse(tree.root_node):
        if node.type == "struct_item" or node.type == "enum_item":
            name = get_name(node, "type_identifier")
            methods = []
            class_to_methods[name] = methods
            class_info.append(
                {
                    "name": name,
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                    "methods": methods,
                }
            )
        elif node.type == "impl_item":
            class_ = get_type(node.child_by_field_name("type"))
            methods = class_to_methods.get(class_, None)
            if methods is not None:
                for child in traverse(node):
                    if child.type == "function_item":
                        methods.append(
                            {
                                "name": get_name(child),
                                "start_line": child.start_point.row,
                                "end_line": child.end_point.row,
                                "text": child.text.decode("utf-8").splitlines(),
                            }
                        )
        elif node.type == "function_item":
            function_names.append(
                {
                    "name": get_name(node),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )

    return class_info, function_names, file_content.splitlines()


def parse_cpp_file(file_path, file_content=None):
    """Parse a Cpp file to extract class and function definitions with their line numbers.
    :param file_path: Path to the Python file.
    :return: Class names, function names, and file contents
    """
    parser = Parser(Language(tscpp.language()))

    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""
    else:
        try:
            tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""

    class_info = []
    function_names = []

    def get_type(node: Node):
        if node.type == "type_identifier":
            return node.text.decode("utf-8")
        elif node.type == "template_type":
            return get_type(node.child_by_field_name("name"))
        return None

    for node in traverse(tree.root_node):
        if node.type == "class_specifier":
            methods = []
            if file_path.endswith(".c"):
                continue
            class_info.append(
                {
                    "name": get_type(node.child_by_field_name("name")),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                    "methods": methods,
                }
            )
            for child in traverse(node):
                if child.type == "function_definition":
                    name_node = child.child_by_field_name("declarator")
                    name_node = name_node.child_by_field_name("declarator")
                    if name_node is None:
                        continue
                    methods.append(
                        {
                            "name": name_node.text.decode("utf-8"),
                            "start_line": child.start_point.row,
                            "end_line": child.end_point.row,
                            "text": child.text.decode("utf-8").splitlines(),
                        }
                    )
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("declarator")
            name_node = name_node.child_by_field_name("declarator")
            if name_node is None:
                continue

            in_class = False
            tmp = node
            while tmp != tree.root_node:
                if tmp.type == "class_specifier":
                    in_class = True
                    break
                tmp = tmp.parent
            if in_class:
                continue

            function_names.append(
                {
                    "name": name_node.text.decode("utf-8"),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )

    return class_info, function_names, file_content.splitlines()


def parse_xml_file(file_path, file_content=None):
    """Parse an XML file to extract tag names and their line numbers.
    :param file_path: Path to the XML file.
    :return: Tag info and file contents
    """
    if file_content is None:
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                file_content = file.read()
        except Exception as e:
            print(f"Error in file {file_path}: {e}")
            return [], ""
    tag_info = []
    try:
        tree = ET.ElementTree(ET.fromstring(file_content))
        for elem in tree.iter():
            tag_info.append(
                {
                    "tag": elem.tag,
                    "attrib": elem.attrib,
                    "text": elem.text,
                }
            )
    except Exception as e:
        print(f"Error parsing XML in file {file_path}: {e}")
        return [], file_content.splitlines()
    return tag_info, file_content.splitlines()


def parse_typescript_file(file_path, file_content=None):
    """Parse a Typescript file to extract interface definitions and class definitions with their line numbers.
    :param file_path: Path to the Java file.
    :return: Class names, function names, and file contents
    """
    parser = Parser(Language(tsts.language_typescript()))

    if file_content is None:
        try:
            with open(file_path, "r") as file:
                file_content = file.read()
                tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""
    else:
        try:
            tree = parser.parse(bytes(file_content, "utf-8"))
        except Exception as e:  # Catch all types of exceptions
            print(f"Error in file {file_path}: {e}")
            return [], [], ""

    class_info = []
    function_names = []
    arrow_function_idx = 0

    for node in traverse(tree.root_node):
        if node.type == "class_declaration":
            methods = []
            class_info.append(
                {
                    "name": node.child_by_field_name("name").text.decode("utf-8"),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                    "methods": methods,
                }
            )
            for child in traverse(node):
                if child.type == "method_definition":
                    methods.append(
                        {
                            "name": child.child_by_field_name("name").text.decode(
                                "utf-8"
                            ),
                            "start_line": child.start_point.row,
                            "end_line": child.end_point.row,
                            "text": child.text.decode("utf-8").splitlines(),
                        }
                    )
        elif node.type == "function_declaration":
            function_names.append(
                {
                    "name": node.child_by_field_name("name").text.decode("utf-8"),
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )
        elif node.type == "arrow_function":
            function_names.append(
                {
                    "name": f"arrow_function_{arrow_function_idx}",
                    "start_line": node.start_point.row,
                    "end_line": node.end_point.row,
                    "text": node.text.decode("utf-8").splitlines(),
                }
            )
            arrow_function_idx = arrow_function_idx + 1

    return class_info, function_names, file_content.splitlines()


def check_file_ext(file_name, language):
    exts = {
        "cpp": ["h", "hpp", "hxx", "c", "cpp", "cc", "cxx"],
        "typescript": ["js", "ts"],
    }
    file_name = file_name.lower()
    for ext in exts[language]:
        if file_name.endswith(f".{ext}"):
            return True
    return False


def create_structure(directory_path):
    """Create the structure of the repository directory by parsing Python files.
    :param directory_path: Path to the repository directory.
    :return: A dictionary representing the structure.
    """
    structure = {}
    exclude_patterns = {
        "AUTHORS",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "README.md",
        "LICENSE",
        "doc",
    }

    def build_structure(current_path: str):
        """Build the structure of the repository directory.
        :param current_path: Path to the current directory.
        :return: A dictionary representing the structure.
        """
        current_struct = {}
        try:
            with os.scandir(current_path) as entries:
                for entry in entries:
                    if entry.name in exclude_patterns:
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        # Recursively build structure for subdirectories
                        sub_struct = build_structure(entry.path)
                        if sub_struct:  # Only keep non-empty subdirectories
                            current_struct[entry.name] = sub_struct
                    else:
                        # For files, None indicates no need to deep dive
                        current_struct[entry.name] = None
        except PermissionError:
            pass
        return current_struct

    # Start building structure from the root directory
    root_name = os.path.basename(directory_path)
    structure[root_name] = build_structure(directory_path)

    return structure


def get_project_structure_from_local(code_folder):
    assert os.path.exists(code_folder), f"{code_folder} does not exist"

    structure = create_structure(code_folder)
    d = {
        "repo": os.path.basename(code_folder),
        "structure": structure,
    }
    return d
