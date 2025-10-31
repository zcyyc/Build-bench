import yaml
import os
import requests
from requests.auth import HTTPBasicAuth


def upload_file_to_obs(
    obs_url: str,
    username: str,
    password: str,
    project: str,
    package: str,
    local_file_path: str,
    target_filename: str,
) -> None:
    """
    Upload local files to OBS project.

    Args:
        obs_url: OBS API base URL (e.g., "https://api.opensuse.org")
        username: OBS username
        password: OBS password
        project: Target project name (e.g., "home:your_username")
        package: Target package name under the project
        local_file_path: Absolute or relative path to the local file
        target_filename: Desired filename after upload to OBS
    """
    # Build OBS API endpoint URL (upload to source service)
    url = f"{obs_url}/source/{project}/{package}/{target_filename}"

    try:
        # Read local file content
        with open(local_file_path, "rb") as f:
            file_content = f.read()

        # Send PUT request to upload file (using Basic Auth)
        response = requests.put(
            url,
            auth=HTTPBasicAuth(username, password),
            data=file_content,
            headers={
                "Content-Type": "application/octet-stream",
                "Accept": "application/xml",
            },
            timeout=600,
        )

        # Handle response
        if response.status_code in (200, 201):
            return f"Success: File {target_filename} uploaded to OBS successfully."

        else:
            return f"Error: File {target_filename} uploaded to OBS failed. Status code: {response.status_code}, Error message: {response.text}"

    except FileNotFoundError:
        return f"Error: Local file not found - {local_file_path}"
    except requests.exceptions.RequestException as e:
        return f"Error: Request exception - {str(e)}"


def main_upload(package_name, file_name):
    # config info
    with open("config/info.yaml", "r") as f:
        infos = yaml.safe_load(f)
    obs_api_url = infos["user"]["obs_api_url"]
    obs_username = infos["user"]["user_name"]
    obs_password = infos["user"]["password"]
    target_project = infos["user"]["target_project"]

    for file in os.listdir(file_name):
        print(file)
        file_path = os.path.join(file_name, file)
        try:
            upload_file_to_obs(
                obs_url=obs_api_url,
                username=obs_username,
                password=obs_password,
                project=target_project,
                package=package_name,
                local_file_path=file_path,
                target_filename=file,
            )
        except Exception as e:
            print(f"Error: {str(e)}")
            return f"Error: {str(e)}"
    return f"Success: File {file_name} uploaded to OBS {package_name} successfully."
