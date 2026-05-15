from __future__ import annotations
import gzip
import os
from pathlib import Path
import shutil
import sys
import time

import boto3
import paramiko

from .config import REGION



def configure_local_aws_credentials() -> None:
    """Point boto3 at the repo-local AWS credentials file when it exists."""
    root_dir = Path(__file__).resolve().parents[1]
    credentials_file = root_dir / ".aws" / "credentials"
    config_file = root_dir / ".aws" / "config"

    if credentials_file.exists() and not os.environ.get("AWS_SHARED_CREDENTIALS_FILE"):
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials_file)

    if config_file.exists() and not os.environ.get("AWS_CONFIG_FILE"):
        os.environ["AWS_CONFIG_FILE"] = str(config_file)


def get_clients(services: list[str]) -> tuple[boto3.client, ...]:
    session = boto3.Session(region_name=REGION)
    return tuple(session.client(service) for service in services)


def get_latest_amazon_linux_ami(region) -> str:
    """
    Fetches the latest Amazon Linux 2 AMI ID for the configured region.
    """
    ssm = boto3.client("ssm", region_name=region)
    param = ssm.get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"
    )
    ami_id = param["Parameter"]["Value"]
    print(f"    Resolved AMI: {ami_id}")
    return ami_id

# ---------------------------------------------------------------------------
# SSH / SFTP
# ---------------------------------------------------------------------------

def connect_ssh(public_ip: str, ssh_user: str,
                private_key_path: str,
                retries: int = 5,
                retry_wait: int = 10) -> paramiko.SSHClient:
    """
    Opens an SSH connection and returns the connected Paramiko client.
    Retries up to `retries` times to handle slow SSH daemon startup.
    """
    key_path = os.path.expanduser(private_key_path)
    key = paramiko.RSAKey.from_private_key_file(key_path)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    for attempt in range(1, retries + 1):
        try:
            client.connect(hostname=public_ip, username=ssh_user,
                           pkey=key, timeout=10)
            print(f"  [ssh]    Connected to {public_ip}")
            return client
        except Exception as e:
            print(f"  [ssh]    Attempt {attempt}/{retries} failed: {e}. "
                  f"Retrying in {retry_wait}s ...")
            time.sleep(retry_wait)

    print("ERROR: Could not connect via SSH after all attempts.")
    sys.exit(1)


def compress(path: str) -> str:
    """
    Compresses a file with gzip and returns the .gz path.
    """
    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return gz_path


def upload_files(ssh_client: paramiko.SSHClient,
                 files: list[tuple[str, str]]) -> None:
    """
    Uploads files to the remote instance via SFTP.

    Parameters
    ----------
    files : list of (local_path, remote_path) pairs
    """
    sftp = ssh_client.open_sftp()
    sftp.get_channel().setblocking(True)
    sftp.MAX_PACKET_SIZE = 32768
    for local_path, remote_path in files:
        if not os.path.isfile(local_path):
            print(f"ERROR: Local file not found: '{local_path}'")
            sftp.close()
            sys.exit(1)
        size_kb = os.path.getsize(local_path) / 1024
        print(f"  [upload] {local_path} → {remote_path} ({size_kb:.1f} KB)")
        sftp.put(local_path, remote_path)
    sftp.close()


def run_remote(ssh_client: paramiko.SSHClient, command: str) -> str:
    """
    Executes a command on the remote instance.
    Prints stderr if non-empty, and returns stdout as a string.
    """
    print(f"  [run]    {command}")
    _, stdout, stderr = ssh_client.exec_command(command)

    output = stdout.read().decode("utf-8").strip()
    errors = stderr.read().decode("utf-8").strip()

    if errors:
        print(f"  [stderr]\n{errors}")

    return output