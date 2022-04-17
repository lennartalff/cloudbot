import collections
from concurrent.futures import thread
import configparser
import logging
import os
import shlex
import subprocess
from datetime import datetime
import time
import yaml
from functools import wraps

import telegram
import telegram.ext
from croniter import croniter
import threading
import copy
from enum import Enum, auto
from typing import List


class Event():

    def __init__(self) -> None:
        self.started = threading.Event()
        self.emit_started = self.started.set
        self.finished = threading.Event()
        self.__lock = threading.RLock()

    @property
    def result(self):
        with self.__lock:
            return copy.deepcopy(self.__result)

    @result.setter
    def result(self, result):
        with self.__lock:
            self.__result = copy.deepcopy(result)

    def emit_finished(self, result=None):
        self.result = result
        self.finished.set()

    def clear(self):
        self.result = None
        for name in dir(self):
            attr = self.__getattribute__(name)
            if isinstance(attr, threading.Event):
                attr.clear()


class ProgressEvent(Event):

    def __init__(self) -> None:
        super().__init__()
        self.progress = threading.Event()

    @property
    def progress_value(self):
        with self.__lock:
            return copy.deepcopy(self.__progress_value)

    @progress_value.setter
    def progress_value(self, progress):
        with self.__lock:
            self.__progress_value = min(1.0, max(0.0, float(progress)))

    def emit_progress(self, progress):
        self.progress_value = progress
        self.progress.set()


class BaseEvents():

    def __init__(self) -> None:
        pass

    def clear_all(self):
        for name in dir(self):
            attr = self.__getattribute__(name)
            if isinstance(attr, Event):
                attr.clear()


class BackupEvents(BaseEvents):

    def __init__(self) -> None:
        super().__init__()
        self.enable_maintenance = Event()
        self.disable_maintenance = Event()
        self.database_backup = Event()
        self.data_backup = ProgressEvent()
        self.backup = Event()


class BackupThread(threading.Thread):

    def __init__(self, nextcloud_dir, backup_dir, database_name) -> None:
        super().__init__(daemon=True)
        self.nextcloud_dir = nextcloud_dir
        self.backup_base_dir = backup_dir
        self.database_name = database_name
        self.backup_subdir = ""
        self.backup_dir = ""
        self.events = BackupEvents()
        self._send_message = None

    def set_message_callback(self, fun):
        self._send_message = fun

    def send_message(self, msg):
        try:
            self._send_message(msg)
        except TypeError:
            pass

    def run(self) -> None:
        self.events.clear_all()
        failed = False
        self.backup_subdir = datetime.utcnow().strftime("%Y-%m-%d-%H:%M:%S")
        self.backup_dir = os.path.join(self.backup_base_dir,
                                       self.backup_subdir)
        try:
            os.mkdir(self.backup_dir)
        except FileExistsError:
            msg = f"Backup directory '{self.backup_dir}' already existing!"
            logging.error(msg)
            self.send_message(msg)
            failed = True
        except FileNotFoundError:
            msg = f"Backup directory '{self.backup_base_dir}' does not exist!"
            logging.error(msg)
            self.send_message(msg)
            failed = True

        if failed:
            self.events.backup.emit_finished(result=False)
            return

        self.events.enable_maintenance.emit_started()
        success, stdout, stderr = self.enable_maintenance()
        self.events.enable_maintenance.emit_finished(success)
        if not success:
            msg = f"Failed to enter maintenance mode!\n{stderr}"
            logging.error(msg)
            self.send_message(msg)
            self.events.backup.emit_finished(result=False)
            return

        self.events.database_backup.emit_started()
        success, stdout, stderr = self.mysql_dump()
        self.events.database_backup.emit_finished(result=success)
        if not success:
            failed = True
            msg = f"Failed to dump database!\n{stderr}"
            logging.error(msg)
            self.send_message(msg)
        else:
            self.events.data_backup.emit_started()
            success = self.rsync()
            self.events.data_backup.emit_finished(result=success)
            if not success:
                failed = True
                msg = f"Failed to backup data directory!\n{stderr}"
                logging.error(msg)
                self.send_message(msg)

        self.events.disable_maintenance.emit_started()
        success, stdout, stderr = self.disable_maintenance()
        self.events.disable_maintenance.emit_finished(result=success)
        if not success:
            failed = True

        self.events.backup.emit_finished(result=not failed)

    def mysql_dump(self):
        path = os.path.join(self.backup_dir, "nextcloud-sqlbkp.bak")
        logging.info("Creating SQL dump.")
        cmd = ("mysqldump --defaults-extra-file=user.cnf --single-transaction "
               "{} > {}".format(self.database_name, path))
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(shlex.split(cmd),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    def rsync(self):
        path = os.path.join(self.backup_dir, "nextcloud-dirbkp")
        logging.info("Creating Data backup!")
        cmd = "rsync -Aax --info=progress2 {} {}".format(
            self.nextcloud_dir, path)
        p = subprocess.Popen(shlex.split(cmd),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    def enable_maintenance(self):
        msg = "Enabling maintenance mode."
        logging.info(msg)
        self.send_message(msg)
        cmd = ("sudo -u www-data /usr/bin/php /var/www/nextcloud/occ "
               "maintenance:mode --on")
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(shlex.split(cmd),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    def disable_maintenance(self):
        logging.info("Disabling maintenance mode")
        cmd = ("sudo -u www-data /usr/bin/php /var/www/nextcloud/occ "
               "maintenance:mode --off")
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(shlex.split(cmd),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr


EXPECT_BACKUP_CONFIRMATION = range(1)


class CmdPermission(Enum):
    OWNER = auto()
    ADMIN = auto()
    USER = auto()
    STRANGER = auto()


class Cmd():

    def __init__(self,
                 name,
                 desc,
                 cb,
                 permission=CmdPermission.OWNER,
                 is_entrypoint=False) -> None:
        self.name = name
        self.desc = desc
        self.cb = cb
        self.permission = permission
        self.is_entrypoint = is_entrypoint


def get_cmd(name: str, commands: List[Cmd]):
    return next((item for item in commands if item.name == name), None)

def permission(permission_level):
    def decorator(f):
        @wraps(f)
        def wrapper(self, update: telegram.Update, context):
            user_id = update.effective_user.id
            if self.user_has_permission(user_id, permission_level):
                logging.info("User %s has sufficient permission level. Has %s and requires %s.", update.effective_user.name, self.user_permission_level(user_id), permission_level)
                return f(self, update, context)
            else:
                logging.critical("User %s has not the required permission. User has %s but needs %s.", update.effective_user.name, self.user_permission_level(user_id), permission_level)
                return telegram.ext.ConversationHandler.END

        return wrapper
    return decorator

class Bot():

    def __init__(self) -> None:
        self.read_config()

        self.updater = telegram.ext.Updater(self.bot_token)
        self.dispatcher = self.updater.dispatcher
        self.commands = [
            Cmd(name="next",
                desc="Date of next scheduled backup.",
                cb=self.cmd_next,
                permission=CmdPermission.USER),
            Cmd(name="backup",
                desc="Start a backup manually",
                cb=self.cmd_backup,
                permission=CmdPermission.ADMIN,
                is_entrypoint=True),
            Cmd(name="start",
                desc="Start the bot.",
                cb=self.cmd_start,
                permission=CmdPermission.STRANGER),
            Cmd(name="cancel",
                desc="Cancel your action",
                cb=self.cmd_cancel,
                permission=CmdPermission.USER)
        ]
        self.add_commands()

    

    def add_commands(self):
        cmds = []

        for cmd in self.commands:
            if not cmd.is_entrypoint:
                self.dispatcher.add_handler(
                    telegram.ext.CommandHandler(command=cmd.name,
                                                callback=cmd.cb))
            cmds.append(telegram.bot.BotCommand(cmd.name, cmd.desc))

        cmd = get_cmd("backup", self.commands)
        fallback_cmd = get_cmd("cancel", self.commands)
        conv_handler = telegram.ext.ConversationHandler(
            entry_points=[telegram.ext.CommandHandler(cmd.name, cmd.cb)],
            states={
                EXPECT_BACKUP_CONFIRMATION: [
                    telegram.ext.MessageHandler(telegram.ext.Filters.text,
                                                self.backup_button_handler)
                ],
                telegram.ext.ConversationHandler.TIMEOUT: [
                    telegram.ext.MessageHandler(
                        telegram.ext.Filters.text
                        | telegram.ext.Filters.command,
                        self.conversation_timeout)
                ],
            },
            fallbacks=[
                telegram.ext.CommandHandler(fallback_cmd.name, fallback_cmd.cb)
            ],
            conversation_timeout=10)
        self.dispatcher.add_handler(conv_handler)
        self.updater.bot.set_my_commands(cmds)

    def conversation_timeout(self, update: telegram.Update, context: telegram.ext.CallbackContext):
        try:
            update.message.reply_text("Conversation timeout.")
        except telegram.TelegramError as e:
            logging.error("%s", e)

    def backup_button_handler(self, update: telegram.Update, context):
        text = update.message.text.lower()
        if text == "yes":
            msg = "Not implemented to do backups on user request!"
        elif text == "no":
            msg = "Maybe the next time..."
        else:
            update.message.reply_text("Did not expect that reply...\n"
                                      "Maybe use the keyboard next time?")
            return telegram.ext.ConversationHandler.END
        update.message.reply_text(msg)
        return telegram.ext.ConversationHandler.END

    def cmd_next(self, update: telegram.Update,
                 context: telegram.ext.CallbackContext):
        try:
            update.message.reply_text("Not implemented.")
        except telegram.TelegramError as e:
            logging.error("%s", e)

    def cmd_cancel(self, update: telegram.Update,
                   context: telegram.ext.CallbackContext):
        try:
            update.message.reply_text("Canceled.")
        except telegram.TelegramError as e:
            logging.error("%s", e)
        return telegram.ext.ConversationHandler.END

    @permission(CmdPermission.ADMIN)
    def cmd_backup(self, update: telegram.Update,
                   context: telegram.ext.CallbackContext):
        if self.handle_unknown_user(update):
            return telegram.ext.ConversationHandler.END
        keyboard = [[telegram.KeyboardButton("Yes")],
                    [telegram.KeyboardButton("No")]]
        reply_markup = telegram.ReplyKeyboardMarkup(keyboard,
                                                    one_time_keyboard=True)
        try:
            update.message.reply_text("Are you sure?",
                                      reply_markup=reply_markup)
        except telegram.TelegramError as e:
            logging.error("%s", e)
        return EXPECT_BACKUP_CONFIRMATION

    def cmd_help(self, update: telegram.Update,
                 context: telegram.ext.CallbackContext):
        if self.handle_unknown_user(update):
            return
        try:
            update.message.reply_text(
                "I would help you. But I don't know how!")
        except telegram.TelegramError as e:
            logging.error("%s", e)

    def handle_unknown_user(self, update: telegram.Update):
        if not self.is_user_known(update.effective_user.id):
            try:
                update.message.reply_text("I do not talk to strangers.")
            except telegram.TelegramError as e:
                logging.error("%s", e)
            self.notify_of_unknown_user(
                update.effective_user.mention_markdown_v2())
            return True
        return False

    def read_config(self):
        config = configparser.ConfigParser()
        config.read('settings.conf')
        self.bot_token = config["TELEGRAM"]["token"]
        self.backup_dir = config["GENERAL"]["backup_dir"]
        if not os.path.isdir(self.backup_dir):
            logging.error(
                "Backup directory does not exists! Check your settings.conf")
            exit(1)
        self.db_name = config["GENERAL"]["database"]
        self.cron_str = config["GENERAL"]["schedule"]
        self.interval = int(config["GENERAL"]["update_interval"])
        with open("known_telegram_ids.yaml", "r") as f:
            self.known_users = yaml.safe_load(f)["known_users"]

    def cmd_start(self, update: telegram.Update,
                  context: telegram.ext.CallbackContext):
        user = update.effective_user
        if self.is_user_known(user.id):
            msg = fr"Hi {user.mention_markdown_v2()}\!"
            update.message.reply_markdown_v2(
                msg, reply_markup=telegram.ForceReply(selective=True))
        else:
            update.message.reply_text("I do not talk to strangers.")
            self.notify_of_unknown_user(user.mention_markdown_v2())

    def notify_of_unknown_user(self, user_mention):
        owner_id = self.owner_id()
        msg = fr"Got message from unknwon user {user_mention}\!"
        if owner_id:
            try:
                self.updater.bot.send_message(owner_id, msg,
                                              telegram.ParseMode.MARKDOWN_V2)
            except telegram.TelegramError as e:
                logging.error("%s", e)

    def is_user_known(self, user_id):
        if any(d["id"] == user_id for d in self.known_users):
            return True
        return False

    def user_permission_level(self, user_id):
        logging.debug("Checking permission level for user %d", user_id)
        role = next((item["role"] for item in self.known_users if item["id"] == user_id), None)
        if role is None:
            return None
        role = role.upper()
        return CmdPermission[role]
    
    def user_has_permission(self, user_id, required_permission):
        if not self.is_user_known(user_id):
            return False
        user_level = self.user_permission_level(user_id)
        if required_permission.value < user_level.value:
            return False
        return True

    def owner_id(self):
        return next((item["id"]
                     for item in self.known_users if item["role"] == "owner"),
                    None)


def main():
    name = datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
    logging.basicConfig(
        filename="logs/{}.log".format(name),
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    b = Bot()
    b.updater.start_polling()
    b.updater.idle()

    # itr = croniter(cron_str, time.time())
    # print(itr.get_next())
    # tnext = itr.get_next()
    # while True:
    #     now = time.time()
    #     dt = tnext - now
    #     if (dt > 0):
    #         logging.debug("Need to wait {:d} seconds".format(int(dt)))
    #         dt = min(dt, interval)
    #         logging.debug("Sleeping for {:d} seconds".format(int(dt)))
    #         time.sleep(dt)
    #     else:
    #         do_backup(backup_dir=backup_dir, db_name=db_name)
    #         tnext = croniter(cron_str, time.time()).get_next()


if __name__ == "__main__":
    main()
