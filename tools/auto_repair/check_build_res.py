import os
import requests
from requests.auth import HTTPBasicAuth
from xml.etree import ElementTree
import time
import yaml


def download_logs_and_sources(temp_dir, base_url, user_name, password):
    log_url = f"{base_url}/_log"
    response = requests.get(
        log_url,
        auth=HTTPBasicAuth(user_name, password),
        headers={"Accept": "application/xml"},
        timeout=600,
    )
    response.raise_for_status()

    try:
        if "temp" in temp_dir:
            with open(os.path.join(temp_dir, "log_failed.txt"), "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return os.path.join(temp_dir, "log_failed.txt")
        else:
            return None
    except Exception as e:
        return None


def check_main(temp_dir: str, package_name: str):
    with open("config/info.yaml", "r") as f:
        infos = yaml.safe_load(f)
    obs_url = infos["user"]["obs_api_url"]
    user_name = infos["user"]["user_name"]
    password = infos["user"]["password"]
    project = infos["user"]["target_project"]
    repository_name = infos["user"]["repository_name"]
    architecture_name = infos["user"]["architecture_name"]

    max_wait_seconds = 180
    check_interval = 30
    elapsed_seconds = 0

    base_url = f"{obs_url}/build/{project}/{repository_name}/{architecture_name}/{package_name}/"
    status_url = base_url + "_status"

    while elapsed_seconds < max_wait_seconds:
        try:
            response = requests.get(
                status_url,
                auth=HTTPBasicAuth(user_name, password),
                headers={"Accept": "application/xml"},
                timeout=600,
            )
            response.raise_for_status()

            root = ElementTree.fromstring(response.text)
            print("root.attrib:\n", root.attrib)

            code_value = root.attrib.get("code")

            if code_value != "building":
                if code_value == "broken":
                    return f"Build broken! The sources either contain no build description (e.g. specfile), automatic source processing failed or a merge conflict does exist. Repository has been published. \n broken: can not parse name from {package_name}.spec"
                elif code_value == "unresolvable":
                    return "Build unresolvable! The build can not begin, because required packages are either missing or not explicitly defined."
                elif code_value == "succeeded":
                    return "Build succeeded! The build has been successfully completed."
                else:
                    log_path = download_logs_and_sources(
                        temp_dir, base_url, user_name, password
                    )
                    if log_path is None:
                        return "Build failed! The failed log has been updated."
                    return (
                        f"Build failed! The failed log has been updated to: {log_path}"
                    )

            time.sleep(check_interval)
            elapsed_seconds += check_interval

        except requests.exceptions.RequestException as e:
            print(f"Check build status failed: {str(e)}. Will retry in 10 seconds.")
            time.sleep(10)
            elapsed_seconds += 10
            continue

    return f"Build timeout! The build has not been completed within {max_wait_seconds} seconds. Default build failed."
