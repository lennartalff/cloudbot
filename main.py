import configparser
import logging
import os
import shlex
import subprocess
from datetime import datetime
import time
import yaml

import telegram
import telegram.ext
from croniter import croniter


class Backup():

    def __init__(self) -> None:
        self.read_config()

        self.bot_updater = telegram.ext.Updater(self.bot_token)
        self.bot_dispatcher = self.bot_updater.dispatcher
        self.bot_dispatcher.add_handler(
            telegram.ext.CommandHandler("start", self.bot_cmd_start))

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

    def bot_cmd_start(self, update: telegram.Update,
                      context: telegram.ext.CallbackContext):
        user = update.effective_user
        if not self.user_is_known(user.id):
            update.message.reply_text("I do not talk to strangers.")
        else:
            msg = fr"Hi {user.mention_markdown_v2()}\!"
            update.message.reply_markdown_v2(
                msg, reply_markup=telegram.ForceReply(selective=True))
            self.notify_of_unknown_user(user.mention_markdown_v2())

    def notify_of_unknown_user(self, user_mention):
        owner_id = self.owner_id()
        msg = fr"Got message from unknwon user {user_mention}\!"
        if owner_id:
            try:
                self.bot_updater.bot.send_message(
                    owner_id, msg, telegram.ParseMode.MARKDOWN_V2)
            except telegram.TelegramError as e:
                logging.error("%s", e)

    def user_is_known(self, user_id):
        if any(d["id"] == user_id for d in self.known_users):
            return True
        return False

    def owner_id(self):
        return next((item["id"]
                     for item in self.known_users if item["role"] == "owner"),
                    None)

    @staticmethod
    def mysql_dump(backup_dir, db_name):
        path = os.path.join(backup_dir, "nextcloud-sqlbkp.bak")
        logging.info("Creating SQL dump.")
        cmd = ("mysqldump --defaults-extra-file=user.cnf --single-transaction "
               "{} > {}".format(db_name, path))
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True,
                             shell=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    @staticmethod
    def rsync(backup_dir, nextcloud_dir):
        path = os.path.join(backup_dir, "nextcloud-dirbkp")
        logging.info("Creating Data backup!")
        cmd = "rsync -Aax --info=progress2 {} {}".format(nextcloud_dir, path)
        p = subprocess.Popen(cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True,
                             shell=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    @staticmethod
    def enable_maintenance():
        logging.info("Enabling maintenance mode")
        cmd = "sudo -u www-data /usr/bin/php /var/www/nextcloud/occ maintenance:mode --on"
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True,
                             shell=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    @staticmethod
    def disable_maintenance():
        logging.info("Disabling maintenance mode")
        cmd = "sudo -u www-data /usr/bin/php /var/www/nextcloud/occ maintenance:mode --off"
        logging.debug("Executing command: %s", cmd)
        p = subprocess.Popen(cmd,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             universal_newlines=True,
                             shell=True)
        stdout, stderr = p.communicate()
        return (p.returncode == 0), stdout, stderr

    def do_backup(self):
        error = False
        path = os.path.join(self.backup_dir,
                            datetime.now().strftime("%Y-%m-%d-%H:%M:%S"))
        logging.info("Creating backup directory '%s'", path)
        try:
            os.mkdir(path)
        except FileExistsError as error:
            logging.error("Backup directory alreading existing!")
            logging.error("%s", error)
            return False
        except FileNotFoundError:
            logging.error("Backup directory '%s' does not exist!",
                          self.backup_dir)
            return False

        success, stdout, stderr = self.enable_maintenance()
        if not success:
            logging.error("Failed to enter maintenance mode! Stopping...")
            logging.error("STDERR: %s", stderr)
            return False

        success, stdout, stderr = self.mysql_dump(backup_dir=path,
                                                  db_name=self.db_name)
        if not success:
            error = True
            logging.error("Failed to dump database! STDERR: %s", stderr)
        else:
            success, stdout, stderr = self.rsync(
                backup_dir=self.backup_dir, nextcloud_dir=self.nextcloud_dir)
            if not success:
                logging.error("Failed to backup data directory! STDERR: %s",
                              stderr)

        success, stdout, stderr = self.disable_maintenance()
        if not success:
            logging.error("Failed to leave maintenance mode! Stopping...")
            logging.error("STDERR: %s", stderr)
            return False

        return not error


def main():
    name = datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
    logging.basicConfig(
        filename="logs/{}.log".format(name),
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    b = Backup()
    b.bot_updater.start_polling()
    b.bot_updater.idle()

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
