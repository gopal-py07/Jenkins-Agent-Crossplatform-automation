import os
import platform
import subprocess
import time
import psutil
import json
import smtplib
import logging
from email.mime.text import MIMEText
from dotenv import load_dotenv
from pathlib import Path
import signal
import threading

# Load environment variables
load_dotenv()

# Load configuration from JSON file
CONFIG_PATH = 'config.json'
with open(CONFIG_PATH) as config_file:
    config = json.load(config_file)

# Jenkins server details and alert settings
JENKINS_SERVER_URL = config["JENKINS_SERVER_URL"]
ALERT_EMAIL = config["ALERT_SETTINGS"]["EMAIL"]
SMTP_SERVER = config["ALERT_SETTINGS"]["SMTP_SERVER"]
SMTP_PORT = config["ALERT_SETTINGS"]["SMTP_PORT"]
SMTP_USERNAME = config["ALERT_SETTINGS"]["SMTP_USERNAME"]
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Monitoring interval and debug mode
CHECK_INTERVAL = config["MONITORING_INTERVAL"]
DEBUG_MODE = config.get("DEBUG_MODE", False)

# Set up logging
log_dir = Path('logs')
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=log_dir / 'service_monitor.log',
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.DEBUG if DEBUG_MODE else logging.INFO
)

def signal_handler(sig, frame):
    """Handle termination signals gracefully."""
    logging.info('Received shutdown signal, cleaning up...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def validate_configuration():
    """Validate config.json and .env files for all required fields."""
    required_keys = ["JENKINS_SERVER_URL", "AGENT_DETAILS", "ALERT_SETTINGS", "MONITORING_INTERVAL"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required configuration key: {key}")

    if not SMTP_PASSWORD:
        raise ValueError("Missing SMTP_PASSWORD in .env")
    
    logging.info("Configuration validated successfully.")

def get_agent_details():
    """Get specific agent details based on the platform."""
    system_platform = platform.system()
    agent_details = config["AGENT_DETAILS"].get(system_platform)

    if system_platform == "Linux":
        AGENT_NAME = agent_details["AGENT_NAME"]
        AGENT_SECRET = os.getenv("LINUX_AGENT_SECRET")
        AGENT_WORKDIR = agent_details["AGENT_WORKDIR"]

    elif system_platform == "Windows":
        AGENT_NAME = agent_details["AGENT_NAME"]
        AGENT_SECRET = os.getenv("WINDOWS_AGENT_SECRET")
        AGENT_WORKDIR = agent_details["AGENT_WORKDIR"]

    else:
        raise Exception("Unsupported platform. Only Windows and Linux are supported.")

    if not AGENT_SECRET:
        raise ValueError(f"Agent secret for {system_platform} not found in .env")

    return AGENT_NAME, AGENT_SECRET, AGENT_WORKDIR

def run_command_with_retry(command, error_message, retries=3, delay=5):
    """Run a system command with retry mechanism."""
    for attempt in range(retries):
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return result
        except subprocess.CalledProcessError as e:
            logging.error(f"{error_message}: {e}. Attempt {attempt + 1}/{retries}")
            time.sleep(delay)
    logging.critical(f"Command failed after {retries} attempts: {command}")
    raise RuntimeError(f"{error_message}")

def download_jenkins_agent():
    """Download Jenkins agent jar file."""
    logging.info("Downloading Jenkins agent...")
    agent_jar_path = "agent.jar"
    jar_download_url = f"{JENKINS_SERVER_URL}/jnlpJars/agent.jar"
    run_command_with_retry(["curl", "-o", agent_jar_path, jar_download_url], "Failed to download Jenkins agent JAR")
    return agent_jar_path

def install_service_linux(agent_name, agent_secret, agent_workdir, agent_jar_path):
    """Install Jenkins agent as a systemd service on Linux."""
    logging.info("Configuring Jenkins agent as a Linux service...")
    service_script = f"""
    [Unit]
    Description=Jenkins Agent Service
    After=network.target

    [Service]
    ExecStart=/usr/bin/java -jar {os.getcwd()}/{agent_jar_path} -jnlpUrl {JENKINS_SERVER_URL}/computer/{agent_name}/slave-agent.jnlp -secret {agent_secret} -workDir "{agent_workdir}"
    Restart=always
    User={os.getlogin()}

    [Install]
    WantedBy=multi-user.target
    """
    service_path = f"/etc/systemd/system/{agent_name}.service"
    with open(service_path, "w") as f:
        f.write(service_script)
    
    run_command_with_retry(["systemctl", "daemon-reload"], "Failed to reload systemd configuration")
    run_command_with_retry(["systemctl", "enable", agent_name], f"Failed to enable service '{agent_name}'")
    run_command_with_retry(["systemctl", "start", agent_name], f"Failed to start service '{agent_name}'")
    logging.info("Jenkins agent service installed and started on Linux.")

def install_service_windows(agent_name, agent_secret, agent_workdir, agent_jar_path):
    """Install Jenkins agent as a service on Windows using sc.exe."""
    logging.info("Configuring Jenkins agent as a Windows service...")
    service_name = agent_name

    create_service_cmd = [
        "sc.exe", "create", service_name,
        "binPath=", f"\"java -jar {os.path.join(os.getcwd(), agent_jar_path)} -jnlpUrl {JENKINS_SERVER_URL}/computer/{service_name}/slave-agent.jnlp -secret {agent_secret} -workDir {agent_workdir}\"",
        "DisplayName=", f"Jenkins Agent - {service_name}",
        "start=", "auto"
    ]

    run_command_with_retry(create_service_cmd, f"Failed to create Windows service '{service_name}'")
    run_command_with_retry(["sc.exe", "start", service_name], f"Failed to start Windows service '{service_name}'")
    logging.info(f"Jenkins agent service '{service_name}' installed and started on Windows.")

def send_alert_email(subject, message):
    """Send an alert email if the service goes down."""
    logging.info(f"Sending alert email with subject: {subject}")
    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ALERT_EMAIL

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, ALERT_EMAIL, msg.as_string())
        logging.info("Alert email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send alert email: {e}")

def monitor_service(agent_name):
    """Monitor the Jenkins agent service and alert if it goes down."""
    logging.info(f"Starting monitoring for service '{agent_name}'")
    while True:
        try:
            is_active = False
            if platform.system() == "Linux":
                result = run_command_with_retry(["systemctl", "is-active", agent_name], 
                                                f"Failed to check status for Linux service '{agent_name}'")
                is_active = result.stdout.strip() == "active"
            elif platform.system() == "Windows":
                result = run_command_with_retry(["sc.exe", "query", agent_name], 
                                                f"Failed to check status for Windows service '{agent_name}'")
                is_active = "RUNNING" in result.stdout
            if not is_active:
                alert_message = f"ALERT: Jenkins agent service '{agent_name}' is down!"
                logging.warning(alert_message)
                send_alert_email(f"Jenkins Agent Alert: {agent_name}", alert_message)
        except Exception as e:
            logging.error(f"Unexpected error while monitoring '{agent_name}': {e}")
        
        time.sleep(CHECK_INTERVAL)

def main():
    validate_configuration()
    
    # Get platform-specific agent details
    agent_name, agent_secret, agent_workdir = get_agent_details()

    # Download Jenkins agent JAR file
    agent_jar_path = download_jenkins_agent()

    # Install as service based on platform
    system_platform = platform.system()
    if system_platform == "Linux":
        install_service_linux(agent_name, agent_secret, agent_workdir, agent_jar_path)
    elif system_platform == "Windows":
        install_service_windows(agent_name, agent_secret, agent_workdir, agent_jar_path)
    else:
        logging.error("Unsupported platform. This script only supports Windows and Linux.")
        return

    # Monitor the Jenkins agent service
    monitor_service(agent_name)

if __name__ == "__main__":
    main()
