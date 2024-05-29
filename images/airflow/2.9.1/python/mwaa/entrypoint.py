"""
This is the entrypoint of the Docker image when running Airflow components.

The script gets called with the Airflow component name, e.g. scheduler, as the
first and only argument. It accordingly runs the requested Airflow component
after setting up the necessary configurations.
"""

# Setup logging first thing to make sure all logs happen under the right setup. The
# reason for needing this is that typically a `logger` object is defined at the top
# of the module and is used through out it. So, if we import a module before logging
# is setup, its `logger` object will not have the right setup.
# ruff: noqa: E402
# fmt: off
import logging.config
from mwaa.logging.config import (
    LOGGING_CONFIG,
    SCHEDULER_LOGGER_NAME,
    TRIGGERER_LOGGER_NAME,
    WEBSERVER_LOGGER_NAME,
    WORKER_LOGGER_NAME,
)
logging.config.dictConfig(LOGGING_CONFIG)
# fmt: on

# Python imports
from typing import Dict, List
import asyncio
import json
import logging
import os
import sys
import time

# 3rd party imports
import boto3
from botocore.exceptions import ClientError

from mwaa.config.airflow import get_airflow_config
from mwaa.config.sqs import (
    get_sqs_queue_name,
    should_create_queue,
)
from mwaa.logging.config import MWAA_LOGGERS
from mwaa.logging.loggers import CompositeLogger
from mwaa.utils.cmd import run_command
from mwaa.utils.dblock import with_db_lock
from mwaa.utils.process_manager import Subprocess, run_subprocesses


# Usually, we pass the `__name__` variable instead as that defaults to the
# module path, i.e. `mwaa.entrypoint` in this case. However, since this is
# the entrypoint script, `__name__` will have the value of `__main__`, hence
# we hard-code the module path.
logger = logging.getLogger("mwaa.entrypoint")

# TODO Fix the "type: ignore"s in this file.


AVAILABLE_COMMANDS = [
    "webserver",
    "scheduler",
    "worker",
    "triggerer",
    "shell",
    "resetdb",
    "spy",
]


@with_db_lock(1234)
async def airflow_db_init(environ: dict[str, str]):
    """
    Initialize Airflow database.

    Before Airflow can be used, a call to `airflow db migrate` must be done. This
    function does this. This function is called in the entrypoint to make sure that,
    for any Airflow component, the database is initialized before it starts.

    This function uses a DB lock to make sure that no two processes execute this
    function at the same time.

    :param environ: A dictionary containing the environment variables.
    """
    logger.info("Calling 'airflow db migrate' to initialize the database.")
    await run_command("airflow db migrate", env=environ)


@with_db_lock(4321)
async def airflow_db_reset(environ: dict[str, str]):
    """
    Reset Airflow metadata database.

    This function resets the Airflow metadata database. It is called when the `resetdb`
    command is specified.

    :param environ: A dictionary containing the environment variables.
    """
    logger.info("Resetting Airflow metadata database.")
    await run_command("airflow db reset --yes", env=environ)


@with_db_lock(5678)
async def create_airflow_user(environ: dict[str, str]):
    """
    Create the 'airflow' user.

    To be able to login to the webserver, you need a user. This function creates a user
    with default credentials.

    Notice that this should only be used in development context. In production, other
    means need to be employed to create users with strong passwords. Alternatively, with
    MWAA setup, a plugin is employed to integrate with IAM (not implemented yet.)

    :param environ: A dictionary containing the environment variables.
    """
    logger.info("Calling 'airflow users create' to create the webserver user.")
    await run_command(
        "airflow users create "
        "--username airflow "
        "--firstname Airflow "
        "--lastname Admin "
        "--email airflow@example.com "
        "--role Admin "
        "--password airflow",
        env=environ,
    )


@with_db_lock(1357)
def create_queue() -> None:
    """
    Create the SQS required by Celery.

    In our setup, we use SQS as the backend for Celery. Usually, this should be created
    before hand. However, sometimes you might want to create the SQS queue during
    startup. One such example is when using the elasticmq server as a mock SQS server.
    """
    if not should_create_queue():
        return
    queue_name = get_sqs_queue_name()
    endpoint = os.environ.get("MWAA__SQS__CUSTOM_ENDPOINT")
    sqs = boto3.client("sqs", endpoint_url=endpoint)  # type: ignore
    try:
        # Try to get the queue URL to check if it exists
        sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]  # type: ignore
        logger.info(f"Queue {queue_name} already exists.")
    except ClientError as e:
        # If the queue does not exist, create it
        if (
            e.response.get("Error", {}).get("Code")
            == "AWS.SimpleQueueService.NonExistentQueue"
        ):
            response = sqs.create_queue(QueueName=queue_name)  # type: ignore
            queue_url = response["QueueUrl"]  # type: ignore
            logger.info(f"Queue created: {queue_url}")
        else:
            # If there is a different error, raise it
            raise e


async def install_user_requirements(cmd: str, environ: dict[str, str]):
    """
    Install user requirements.

    User requirements should be placed in a requirements.txt file and the environment
    variable `MWAA__CORE__REQUIREMENTS_PATH` should be set to the location of that file.
    In a Docker Compose setup, you would usually want to create a volume that maps a
    requirements.txt file in the host machine somewhere in the container, and then set
    the `MWAA__CORE__REQUIREMENTS_PATH` accordingly.

    :param environ: A dictionary containing the environment variables.
    """
    requirements_file = environ.get("MWAA__CORE__REQUIREMENTS_PATH")
    logger.info(f"MWAA__CORE__REQUIREMENTS_PATH = {requirements_file}")
    if requirements_file and os.path.isfile(requirements_file):
        logger.info(f"Installing user requirements from {requirements_file}...")

        worker = Subprocess(
            cmd=["safe-pip-install", "-r", requirements_file],
            env=environ,
            logger=CompositeLogger(
                "requirements_composite_logging",  # name can be anything unused.
                # We use a set to avoid double logging to console if the user doesn't
                # use CloudWatch for logging.
                *set(
                    [
                        logging.getLogger(MWAA_LOGGERS.get(f"{cmd}_requirements")),
                        logger,
                    ]
                ),
            ),
        )
        worker.start()
    else:
        logger.info("No user requirements to install.")

async def execute_startup_script(cmd: str, environ: Dict[str, str]):
    """
    Execute user startup script.

    :param cmd - The command to run, e.g. "worker".
    :param environ: A dictionary containing the environment variables.
    """
    EXECUTE_STARTUP_SCRIPT_PATH = "/usr/local/airflow/execute-startup.sh"
    STARTUP_SCRIPT_PATH = "/usr/local/airflow/startup/startup.sh"

    if os.path.isfile(STARTUP_SCRIPT_PATH):
        logger.info("Executing startup script.")
        worker = Subprocess(
            cmd=["/bin/bash", EXECUTE_STARTUP_SCRIPT_PATH],
            env=environ,
            logger=logging.getLogger(f"mwaa.{cmd}"),
        )
        worker.start()

        customer_stored_env_path = "/usr/local/airflow/customer_stored_env.json"
        if os.path.isfile(customer_stored_env_path):
            with open(customer_stored_env_path, "r") as f:
                customer_env_dict = json.load(f)
            return customer_env_dict
        else:
            return {}
        
    else:
        logger.info(f"No startup script found at {STARTUP_SCRIPT_PATH}.")
        return {}


def export_env_variables(environ: dict[str, str]):
    """
    Export the environment variables to .bashrc and .bash_profile.

    For Airflow to function properly, a bunch of environment variables needs to be
    defined, which we do in the entrypoint. However, during development, a need might
    arise for bashing into the Docker container and doing some debugging, e.g. running
    a bunch of Airflow CLI commands. This won't be possible if the necessary environment
    variables are not defined, which is the case unless we have them defined in the
    .bashrc/.bash_profile files. This function does exactly that.

    :param environ: A dictionary containing the environment variables to export.
    """
    # Get the home directory of the current user
    home_dir = os.path.expanduser("~")
    bashrc_path = os.path.join(home_dir, ".bashrc")
    bash_profile_path = os.path.join(home_dir, ".bash_profile")

    # Environment variables to append
    env_vars_to_append = [
        # TODO Need to escape value.
        f'export {key}="{value}"\n'
        for key, value in environ.items()
    ]

    # Append to .bashrc
    with open(bashrc_path, "a") as bashrc:
        bashrc.writelines(env_vars_to_append)

    # Append to .bash_profile
    with open(bash_profile_path, "a") as bash_profile:
        bash_profile.writelines(env_vars_to_append)


def create_airflow_subprocess(
    args: List[str], environ: Dict[str, str], logger_name: str
):
    """
    Create a subprocess for an Airflow command.

    Notice that while this function creates the sub-process, it will not start it.
    So, the caller is responsible for starting it.

    :param args - The arguments to pass to the Airflow CLI.
    :param environ - A dictionary containing the environment variables.
    :param logger_name - The name of the logger to use for capturing the generated logs.

    :returns The created subprocess.
    """
    logger: logging.Logger = logging.getLogger(logger_name)
    return Subprocess(cmd=["airflow", *args], env=environ, logger=logger)


def run_airflow_command(cmd: str, environ: Dict[str, str]):
    """
    Run the given Airflow command in a subprocess.

    :param cmd - The command to run, e.g. "worker".
    :param environ: A dictionary containing the environment variables.
    """
    match cmd:
        case "scheduler":
            run_subprocesses(
                [
                    create_airflow_subprocess(
                        [airflow_cmd], environ=environ, logger_name=logger_name
                    )
                    for airflow_cmd, logger_name in [
                        ("scheduler", SCHEDULER_LOGGER_NAME),
                        # Airflow has a dedicated logger for the DAG Processor Manager
                        # So we just use it
                        ("dag-processor", "airflow.processor_manager"),
                        ("triggerer", TRIGGERER_LOGGER_NAME),
                    ]
                ]
            )

        case "worker":
            run_subprocesses(
                [
                    create_airflow_subprocess(
                        ["celery", "worker"],
                        environ=environ,
                        logger_name=WORKER_LOGGER_NAME,
                    ),
                ]
            )

        case "webserver":
            run_subprocesses(
                [
                    create_airflow_subprocess(
                        ["webserver"],
                        environ=environ,
                        logger_name=WEBSERVER_LOGGER_NAME,
                    ),
                ]
            )

        case _:
            raise ValueError(f"Unexpected command: {cmd}")


async def main() -> None:
    """Start execution of the script."""
    try:
        (
            _,
            command,
        ) = sys.argv
        if command not in AVAILABLE_COMMANDS:
            exit(
                f"Invalid command: {command}. "
                f'Use one of {", ".join(AVAILABLE_COMMANDS)}.'
            )
    except Exception as e:
        exit(
            f"Invalid arguments: {sys.argv}. Please provide one argument with one of"
            f'the values: {", ".join(AVAILABLE_COMMANDS)}. Error was {e}.'
        )

    logger.info(f"Warming a Docker container for an Airflow {command}.")

    # Add the necessary environment variables.
    environ = {**os.environ, **get_airflow_config(), "MWAA_COMMAND": command}

    # IMPORTANT NOTE: The level for this should stay "DEBUG" to avoid logging customer
    # custom environment variables, which potentially contains sensitive credentials,
    # to stdout which, in this case of Fargate hosting (like in Amazon MWAA), ends up
    # being captured and sent to the service hosting.
    logger.debug(f"Environment variables: {environ}")

    await airflow_db_init(environ)
    await create_airflow_user(environ)
    create_queue()
    customer_env = await execute_startup_script(command, environ)
    environ = {**customer_env, **environ}
    await install_user_requirements(command, environ)

    # Export the environment variables to .bashrc and .bash_profile to enable
    # users to run a shell on the container and have the necessary environment
    # variables set for using airflow CLI.
    export_env_variables(environ)

    match command:
        case "shell":
            os.execlpe("/bin/bash", "/bin/bash", environ)
        case "spy":
            while True:
                time.sleep(1)
        case "resetdb":
            # Perform the resetdb functionality
            await airflow_db_reset(environ)
            # After resetting the db, initialize it again
            await airflow_db_init(environ)
        case "scheduler" | "webserver" | "worker":
            run_airflow_command(command, environ)
        case _:
            raise ValueError(f"Invalid command: {command}")


if __name__ == "__main__":
    asyncio.run(main())
else:
    logger.error("This module cannot be imported.")
    sys.exit(1)
