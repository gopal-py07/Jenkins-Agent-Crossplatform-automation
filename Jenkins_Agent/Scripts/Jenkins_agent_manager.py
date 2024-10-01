import logging
import json
import subprocess
import platform
import os

from pathlib import Path
from dotenv import load_dotenv


def get_platform():
    """Identifies the OS platform (Windows or Linux)."""
    return platform.system().lower()


def setup_logging(log_file_path="../../logs/jenkins_agent_manager.log", log_level=logging.DEBUG):
    """Sets up logging to both a file and console."""
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)

    log_path = Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file_path, mode='a')
    file_handler.setLevel(log_level)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info("Logging is set up. Logging to file and console.")
    return logger


def load_env_file(logger):
    """Load .env file and handle errors."""
    try:
        load_dotenv()
        logger.info("Successfully loaded .env file.")
    except Exception as e:
        logger.error("Error loading .env file")
        raise RuntimeError('Failed to load .env file. Please ensure it exists and is correctly formatted.')


def load_config_file(logger, CONFIG_PATH):
    """Load and parse the config file."""
    try:
        config_file_path = Path(CONFIG_PATH)
        if not config_file_path.exists():
            raise FileNotFoundError(f'Config file not found: {config_file_path}')
        with open(config_file_path, 'r') as config_file:
            config = json.load(config_file)
            logger.info(f"Successfully loaded the config file from {CONFIG_PATH}")
            return config
        
    except FileNotFoundError as fnf_error:
        logger.error(f"Config file error: {fnf_error}", exc_info=True)
    except json.JSONDecodeError as json_error:
        logger.error(f"JSON parsing error in config file: {json_error}", exc_info=True)
        raise ValueError("Config file contains invalid JSON. Please check the file format.")
    except Exception as e:
        logger.error(f"Unexpected error loading config file {e}", exc_info=True)
        raise RuntimeError("Failed to load file due to unexpected error.")
    

def validate_configuration(logger, config):
    """Validate config.json and .env files for all required fields and values."""
    required_keys = ["JENKINS_SERVER_URL", "AGENT_DETAILS"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required configuration key: {key}")
        if not config[key]:
            raise ValueError(f"Configuration key '{key}' cannot be empty")

    agent_details = config["AGENT_DETAILS"]
    if not agent_details:
        raise ValueError("AGENT_DETAILS must be a non-empty dictionary")

    if "LINUX" not in agent_details and "WINDOWS" not in agent_details:
        raise ValueError("AGENT_DETAILS must contain keys for both 'Linux' and 'Windows'")

    for platform_key, platform_value in agent_details.items():
        if not platform_value or not all(k in platform_value for k in ["AGENT_NAME", "AGENT_WORKDIR"]):
            raise ValueError(f"Each platform in AGENT_DETAILS ('{platform_key}') must contain 'AGENT_NAME' and 'AGENT_WORKDIR'")
    logger.info("Configuration validated successfully.")


def run_command(command, error_message, logger):
    """Run system command."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='ISO-8859-1')
        logger.info(f"Command output: {result.stdout}")
        logger.info(f"Command error output (if any): {result.stderr}")

        if result.returncode != 0:
            raise RuntimeError(f"{error_message}: {result.stderr}")
        
        logger.info("Command completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"{error_message}: {e}")
        logger.error(f"Command output: {e.stdout}")
        logger.error(f"Command error output: {e.stderr}")
        raise RuntimeError(f"{error_message}")


def download_jenkins_agent(config, logger):
    """Download Jenkins agent jar file."""
    logger.info("Downloading Agent jar...")

    # Set the agent jar path based on the platform
    if get_platform() == "windows":
        agent_jar_path = os.path.join("D:", "jenkins", "agent", "agent.jar")
    else:
        agent_jar_path = os.path.join(os.path.expanduser("~"), "jenkins", "agent.jar")

    # Ensure the directory exists
    jenkins_dir = os.path.dirname(agent_jar_path)
    if not os.path.exists(jenkins_dir):
        os.makedirs(jenkins_dir, exist_ok=True)
    
    # Set appropriate permissions on Linux
    if get_platform() != "windows":
        os.chmod(jenkins_dir, 0o755)

    # Construct the URL for downloading the agent jar
    jar_download_url = f"{config['JENKINS_SERVER_URL']}/jnlpJars/agent.jar"
    
    # Download the Jenkins agent jar using curl
    run_command(["curl", "-o", agent_jar_path, jar_download_url], "Failed to download Jenkins agent jar", logger)
    
    logger.info(f"Jenkins agent jar downloaded to: {agent_jar_path}")
    
    return agent_jar_path


def configure_linux_service(agent_jar_path, config, logger):
    """Configure Jenkins agent as a service on Linux using systemd."""
    # Get the secret from .env
    linux_agent_secret = os.getenv("LINUX_AGENT_SECRET")
    if not linux_agent_secret:
        logger.error("LINUX_AGENT_SECRET is not set in the .env file.")
        raise ValueError("LINUX_AGENT_SECRET is required in the .env file")

    service_file_content = f"""
    [Unit]
    Description=Jenkins Agent
    After=network.target

    [Service]
    ExecStart=/usr/bin/java -jar {agent_jar_path} -url {config['JENKINS_SERVER_URL']} -secret {linux_agent_secret} -name "{config['AGENT_DETAILS']['LINUX']['AGENT_NAME']}" -workDir "{config['AGENT_DETAILS']['LINUX']['AGENT_WORKDIR']}"
    User=gopal
    Restart=always

    [Install]
    WantedBy=multi-user.target
    """
    
    service_file_path = "/etc/systemd/system/jenkins-agent.service"
    
    # Give write permission to the systemd directory temporarily
    try:
        logger.info("Granting write permission to /etc/systemd/system/")
        subprocess.run(["sudo", "chmod", "o+w", "/etc/systemd/system/"], check=True)
        
        # Write the service file
        logger.info("Writing the service file.")
        with open(service_file_path, "w") as service_file:
            service_file.write(service_file_content)

        logger.info("Service file written successfully.")

        # Revert permissions back to secure settings
        logger.info("Reverting write permission for /etc/systemd/system/")
        subprocess.run(["sudo", "chmod", "o-w", "/etc/systemd/system/"], check=True)

        # Reload systemd, enable, and start the service
        run_command(["sudo", "systemctl", "daemon-reload"], "Failed to reload systemd daemon", logger)
        run_command(["sudo", "systemctl", "enable", "jenkins-agent"], "Failed to enable Jenkins agent service", logger)
        run_command(["sudo", "systemctl", "start", "jenkins-agent"], "Failed to start Jenkins agent service", logger)

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to configure Jenkins agent service: {e}")


def configure_windows_service(agent_jar_path, config, logger):
    """Configure Jenkins agent as a service on Windows using sc.exe."""
    # Get the secret from .env
    windows_agent_secret = os.getenv("WINDOWS_AGENT_SECRET")
    if not windows_agent_secret:
        logger.error("WINDOWS_AGENT_SECRET is not set in the .env file.")
        raise ValueError("WINDOWS_AGENT_SECRET is required in the .env file")

    # Define the service name
    service_name = config['AGENT_DETAILS']['WINDOWS']['AGENT_NAME']

    # Define the work directory for Jenkins
    agent_workdir = config['AGENT_DETAILS']['WINDOWS']['AGENT_WORKDIR']

    # Java executable path (assumes JAVA_HOME is set correctly)
    java_path = os.path.join(os.getenv('JAVA_HOME'), "bin", "java.exe")

    # Build the command to register the service
    sc_create_command = [
        "sc.exe", "create", service_name,
        f"binPath= \"{java_path} -jar {agent_jar_path} -url {config['JENKINS_SERVER_URL']} -secret {windows_agent_secret} -name {service_name} -workDir {agent_workdir}\"",
        "start=", "auto"
    ]

    logger.info(f"Registering Jenkins agent as a Windows service with name '{service_name}'")
    run_command(sc_create_command, f"Failed to create Windows service '{service_name}'", logger)

    # Start the service
    sc_start_command = ["sc.exe", "start", service_name]
    run_command(sc_start_command, f"Failed to start Windows service '{service_name}'", logger)


def main():
    logger = setup_logging()
    # Load .env file
    load_env_file(logger)

    # Load config file
    CONFIG_PATH = "../config.json"
    config = load_config_file(logger, CONFIG_PATH)

    validate_configuration(logger, config)

    # Download Jenkins agent
    agent_jar_path = download_jenkins_agent(config, logger)

    # Configure the agent as a service
    if get_platform() == "linux":
        configure_linux_service(agent_jar_path, config, logger)
    elif get_platform() == "windows":
        configure_windows_service(agent_jar_path, config, logger)
    else:
        logger.error("Unsupported platform. Only Windows and Linux are supported.")


if __name__ == "__main__":
    main()
