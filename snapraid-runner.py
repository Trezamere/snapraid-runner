# -*- coding: utf8 -*-
from __future__ import division

import argparse
import configparser
import logging
import logging.handlers
import os.path
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from io import StringIO, IOBase

# Global variables
config = None
email_log = None

OUTPUT = 15
OUTERR = 25


def tee_log(infile: IOBase, out_lines: list, log_level: int) -> threading.Thread:
    """
    Create a thread that saves all the output on infile to out_lines and
    logs every line with log_level
    """

    def tee_thread():
        for line in iter(infile.readline, b""):
            line = line.decode().strip()
            # Do not log the progress display
            if "\r" in line:
                line = line.split("\r")[-1]
            logging.log(log_level, line.strip())
            out_lines.append(line)
        infile.close()

    t = threading.Thread(daemon=True, target=tee_thread)
    t.start()
    return t


def snapraid_command(command: str, args: list = None, ignore_errors: bool = False) -> list:
    """
    Run snapraid command
    Raises subprocess.CalledProcessError if errorlevel != 0
    """
    if args is None:
        args = []
    args.insert(0, config["snapraid"]["executable"])
    args.insert(1, command)
    args.extend(["--conf", config["snapraid"]["config"], "--verbose"])

    p = None
    try:
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
    except FileNotFoundError:
        logging.error("The configured snapraid executable \"{}\" does not "
                      "exist or is not a file".format(config["snapraid"]["executable"]))
        finish(False)
    out = []
    threads = [
        tee_log(p.stdout, out, OUTPUT),
        tee_log(p.stderr, [], OUTERR)]
    for t in threads:
        t.join()
    ret = p.wait()

    # sleep for a while to make prevent output mixup
    time.sleep(0.3)
    if ret == 0 or ignore_errors:
        return out
    else:
        raise subprocess.CalledProcessError(ret, "snapraid " + command)


def send_email(success):
    import smtplib
    from email.mime.text import MIMEText
    from email import charset

    if len(config["smtp"]["host"]) == 0:
        logging.error("Failed to send email because smtp host is not set")
        return

    # use quoted-printable instead of the default base64
    charset.add_charset("utf-8", charset.SHORTEST, charset.QP)
    if success:
        body = "SnapRAID job completed successfully:\n\n\n"
    else:
        body = "Error during SnapRAID job:\n\n\n"

    log = email_log.getvalue()
    maxsize = config['email'].get('maxsize', 500) * 1024
    if maxsize and len(log) > maxsize:
        cut_lines = log.count("\n", maxsize // 2, -maxsize // 2)
        log = (
            "NOTE: Log was too big for email and was shortened\n\n" +
            log[:maxsize // 2] +
            "[...]\n\n\n --- LOG WAS TOO BIG - {} LINES REMOVED --\n\n\n[...]".format(
                cut_lines) +
            log[-maxsize // 2:])
    body += log

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = config["email"]["subject"] + \
                     (" SUCCESS" if success else " ERROR")
    msg["From"] = config["email"]["from"]
    msg["To"] = config["email"]["to"]
    smtp = {"host": config["smtp"]["host"]}
    if config["smtp"]["port"]:
        smtp["port"] = config["smtp"]["port"]
    if config["smtp"]["ssl"]:
        server = smtplib.SMTP_SSL(**smtp)
    else:
        server = smtplib.SMTP(**smtp)
    if config["smtp"]["user"]:
        server.login(config["smtp"]["user"], config["smtp"]["password"])
    server.sendmail(
        config["email"]["from"],
        [config["email"]["to"]],
        msg.as_string())
    server.quit()


def finish(is_success):
    if ("error", "success")[is_success] in config["email"]["sendon"]:
        try:
            send_email(is_success)
        except Exception:
            logging.exception("Failed to send email")

    if is_success:
        logging.info("=" * 60)
        logging.info("Run finished successfully.")
        logging.info("=" * 60 + "\n")
    else:
        logging.error("=" * 60)
        logging.error("Run failed!")
        logging.error("=" * 60 + "\n")
    sys.exit(0 if is_success else 1)


def load_config(args):
    global config
    parser = configparser.RawConfigParser()
    parser.read(args.conf)
    sections = ["snapraid", "logging", "email", "smtp", "scrub"]
    config = dict((x, defaultdict(lambda: "")) for x in sections)
    for section in parser.sections():
        for (k, v) in parser.items(section):
            config[section][k] = v.strip()

    int_options = [
        ("snapraid", "deletethreshold"), ("logging", "maxsize"),
        ("scrub", "percentage"), ("scrub", "older-than"), ("email", "maxsize"),
    ]
    for section, option in int_options:
        try:
            config[section][option] = int(config[section][option])
        except ValueError:
            config[section][option] = 0

    config["smtp"]["ssl"] = (config["smtp"]["ssl"].lower() == "true")
    config["scrub"]["enabled"] = (config["scrub"]["enabled"].lower() == "true")
    config["email"]["short"] = (config["email"]["short"].lower() == "true")
    config["snapraid"]["touch"] = (config["snapraid"]["touch"].lower() == "true")

    if args.scrub is not None:
        config["scrub"]["enabled"] = args.scrub


def setup_logger():
    log_format = logging.Formatter("%(asctime)s [%(levelname)-6.6s] %(message)s")
    root_logger = logging.getLogger()
    logging.addLevelName(OUTPUT, "OUTPUT")
    logging.addLevelName(OUTERR, "OUTERR")
    root_logger.setLevel(OUTPUT)
    console_logger = logging.StreamHandler(sys.stdout)
    console_logger.setFormatter(log_format)
    root_logger.addHandler(console_logger)

    if config["logging"]["file"]:
        log_file = config["logging"]["file"]
        log_dir = os.path.dirname(log_file)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        max_log_size = min(config["logging"]["maxsize"], 0) * 1024
        file_logger = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_log_size,
            backupCount=9)
        file_logger.setFormatter(log_format)
        root_logger.addHandler(file_logger)

    if config["email"]["sendon"]:
        global email_log
        email_log = StringIO()
        email_logger = logging.StreamHandler(email_log)
        email_logger.setFormatter(log_format)
        if config["email"]["short"]:
            # Don't send program stdout in email
            email_logger.setLevel(logging.INFO)
        root_logger.addHandler(email_logger)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config",
                        default="snapraid-runner.conf",
                        dest='conf',
                        metavar="CONFIG",
                        help="Configuration file (default: %(default)s)")
    parser.add_argument("--no-scrub",
                        action='store_false',
                        default=None,
                        dest='scrub',
                        help="Do not scrub (overrides config)")
    args = parser.parse_args()

    if not os.path.exists(args.conf):
        print("snapraid-runner configuration file not found")
        parser.print_help()
        sys.exit(2)

    try:
        load_config(args)
    except Exception:
        print("Unhandled exception while loading config")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        setup_logger()
    except Exception:
        print("Unhandled exception while setting up logging")
        print(traceback.format_exc())
        sys.exit(2)

    try:
        run()
    except Exception:
        logging.exception("Run failed due to unhandled exception:")
        finish(False)


def run():
    logging.info("=" * 60)
    logging.info("Run started")
    logging.info("=" * 60)

    if not os.path.isfile(config["snapraid"]["config"]):
        logging.error("Snapraid config does not exist at " + config["snapraid"]["config"])
        finish(False)

    if config["snapraid"]["touch"]:
        logging.info("=" * 30)
        logging.info("Running touch...")
        logging.info("=" * 30)
        snapraid_command("touch")

    logging.info("=" * 30)
    logging.info("Running diff...")
    logging.info("=" * 30)
    diff_out = snapraid_command("diff", ignore_errors=True)

    diff_results = Counter(line.split(" ")[0] for line in diff_out)
    diff_results = dict((x, diff_results[x]) for x in ["add", "remove", "move", "update"])
    logging.info("*" * 60)
    logging.info(("Diff results: {add} added,  {remove} removed,  "
                  + "{move} moved,  {update} modified").format(**diff_results))
    logging.info("*" * 60)

    if 0 <= config["snapraid"]["deletethreshold"] < diff_results["remove"]:
        logging.error(
            "Deleted files exceed delete threshold of {}, aborting".format(
                config["snapraid"]["deletethreshold"]))
        finish(False)

    if (diff_results["remove"] + diff_results["add"] + diff_results["move"] +
            diff_results["update"] == 0):
        logging.info("No changes detected, no sync required")
    else:
        logging.info("=" * 30)
        logging.info("Running sync...")
        logging.info("=" * 30)
        try:
            snapraid_command("sync")
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)

    if config["scrub"]["enabled"]:
        logging.info("=" * 30)
        logging.info("Running scrub...")
        logging.info("=" * 30)
        try:
            snapraid_command("scrub", [
                "--percentage", str(config["scrub"]["percentage"]),
                "--older-than", str(config["scrub"]["older-than"])
            ])
        except subprocess.CalledProcessError as e:
            logging.error(e)
            finish(False)

    logging.info("All done")
    finish(True)


main()
