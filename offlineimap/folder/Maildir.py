# Maildir folder support
# Copyright (C) 2002-2016 John Goerzen & contributors.
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA

import errno
import socket
import time
import re
import os
from sys import exc_info
from threading import Lock
from hashlib import md5
from offlineimap import OfflineImapError
from .Base import BaseFolder
from email.errors import NoBoundaryInMultipartDefect

# Find the UID in a message filename
re_uidmatch = re.compile(',U=(\d+)')
# Find a numeric timestamp in a string (filename prefix)
re_timestampmatch = re.compile('(\d+)')

timehash = {}
timelock = Lock()


def _gettimeseq(date=None):
    global timehash, timelock
    timelock.acquire()
    try:
        if date is None:
            date = int(time.time())
        if date in timehash:
            timehash[date] += 1
        else:
            timehash[date] = 0
        return date, timehash[date]
    finally:
        timelock.release()


class MaildirFolder(BaseFolder):
    def __init__(self, root, name, sep, repository):
        self.sep = sep  # needs to be set before super().__init__
        super(MaildirFolder, self).__init__(name, repository)
        self.root = root
        # check if we should use a different infosep to support Win file systems
        self.wincompatible = self.config.getdefaultboolean(
            "Account " + self.accountname, "maildir-windows-compatible", False)
        self.infosep = '!' if self.wincompatible else ':'
        """infosep is the separator between maildir name and flag appendix"""
        self.re_flagmatch = re.compile('%s2,(\w*)' % self.infosep)
        # self.ui is set in BaseFolder.init()
        # Everything up to the first comma or colon (or ! if Windows):
        self.re_prefixmatch = re.compile('([^' + self.infosep + ',]*)')
        # folder's md, so we can match with recorded file md5 for validity.
        self._foldermd5 = md5(self.getvisiblename().encode('utf-8')).hexdigest()
        # Cache the full folder path, as we use getfullname() very often.
        self._fullname = os.path.join(self.getroot(), self.getname())
        # Modification time from 'Date' header.
        utime_from_header_global = self.config.getdefaultboolean(
            "general", "utime_from_header", False)
        self._utime_from_header = self.config.getdefaultboolean(
            self.repoconfname, "utime_from_header", utime_from_header_global)
        # What do we substitute pathname separator in names (if any)
        self.sep_subst = '-'
        if os.path.sep == self.sep_subst:
            self.sep_subst = '_'

    # Interface from BaseFolder
    def getfullname(self):
        """Return the absolute file path to the Maildir folder (sans cur|new)"""
        return self._fullname

    # Interface from BaseFolder
    def get_uidvalidity(self):
        """Retrieve the current connections UIDVALIDITY value

        Maildirs have no notion of uidvalidity, so we just return a magic
        token."""
        return 42

    def _iswithintime(self, messagename, date):
        """Check to see if the given message is newer than date (a
        time_struct) according to the maildir name which should begin
        with a timestamp."""

        timestampmatch = re_timestampmatch.search(messagename)
        if not timestampmatch:
            return True
        timestampstr = timestampmatch.group()
        timestamplong = int(timestampstr)
        if timestamplong < time.mktime(date):
            return False
        else:
            return True

    def _parse_filename(self, filename):
        """Returns a messages file name components

        Receives the file name (without path) of a msg.  Usual format is
        '<%d_%d.%d.%s>,U=<%d>,FMD5=<%s>:2,<FLAGS>' (pointy brackets
        denoting the various components).

        If FMD5 does not correspond with the current folder MD5, we will
        return None for the UID & FMD5 (as it is not valid in this
        folder).  If UID or FMD5 can not be detected, we return `None`
        for the respective element.  If flags are empty or cannot be
        detected, we return an empty flags list.

        :returns: (prefix, UID, FMD5, flags). UID is a numeric "long"
            type. flags is a set() of Maildir flags.
        """

        prefix, uid, fmd5, flags = None, None, None, set()
        prefixmatch = self.re_prefixmatch.match(filename)
        if prefixmatch:
            prefix = prefixmatch.group(1)
        folderstr = ',FMD5=%s' % self._foldermd5
        foldermatch = folderstr in filename
        # If there was no folder MD5 specified, or if it mismatches,
        # assume it is a foreign (new) message and ret: uid, fmd5 = None, None

        # XXX: This is wrong behaviour: if FMD5 is missing or mismatches, assume
        # the mail is new and **fix UID to None** to avoid any conflict.

        # XXX: If UID is missing, I have no idea what FMD5 can do. Should be
        # fixed to None in this case, too.

        if foldermatch:
            uidmatch = re_uidmatch.search(filename)
            if uidmatch:
                uid = int(uidmatch.group(1))
        flagmatch = self.re_flagmatch.search(filename)
        if flagmatch:
            flags = set((c for c in flagmatch.group(1)))
        return prefix, uid, fmd5, flags

    def _scanfolder(self, min_date=None, min_uid=None):
        """Cache the message list from a Maildir.

        If min_date is set, this finds the min UID of all messages newer than
        min_date and uses it as the real cutoff for considering messages.
        This handles the edge cases where the date is much earlier than messages
        with similar UID's (e.g. the UID was reassigned much later).

        Maildir flags are:
            D (draft) F (flagged) R (replied) S (seen) T (trashed),
        plus lower-case letters for custom flags.
        :returns: dict that can be used as self.messagelist.
        """

        maxsize = self.getmaxsize()

        retval = {}
        files = []
        nouidcounter = -1  # Messages without UIDs get negative UIDs.
        for dirannex in ['new', 'cur']:
            fulldirname = os.path.join(self.getfullname(), dirannex)
            files.extend((dirannex, filename) for
                         filename in os.listdir(fulldirname))

        date_excludees = {}
        for dirannex, filename in files:
            if filename.startswith('.'):
                continue  # Ignore dot files.
            # We store just dirannex and filename, ie 'cur/123...'
            filepath = os.path.join(dirannex, filename)
            # Check maxsize if this message should be considered.
            if maxsize and (os.path.getsize(
                    os.path.join(self.getfullname(), filepath)) > maxsize):
                continue

            prefix, uid, fmd5, flags = self._parse_filename(filename)
            if uid is None:  # Assign negative uid to upload it.
                uid = nouidcounter
                nouidcounter -= 1
            else:  # It comes from our folder.
                uidmatch = re_uidmatch.search(filename)
                if not uidmatch:
                    uid = nouidcounter
                    nouidcounter -= 1
                else:
                    uid = int(uidmatch.group(1))
            if min_uid is not None and uid > 0 and uid < min_uid:
                continue
            if min_date is not None and not self._iswithintime(filename, min_date):
                # Keep track of messages outside of the time limit, because they
                # still might have UID > min(UIDs of within-min_date). We hit
                # this case for maxage if any message had a known/valid datetime
                # and was re-uploaded because the UID in the filename got lost
                # (e.g. local copy/move). On next sync, it was assigned a new
                # UID from the server and will be included in the SEARCH
                # condition. So, we must re-include them later in this method
                # in order to avoid inconsistent lists of messages.
                date_excludees[uid] = self.msglist_item_initializer(uid)
                date_excludees[uid]['flags'] = flags
                date_excludees[uid]['filename'] = filepath
            else:
                # 'filename' is 'dirannex/filename', e.g. cur/123,U=1,FMD5=1:2,S
                retval[uid] = self.msglist_item_initializer(uid)
                retval[uid]['flags'] = flags
                retval[uid]['filename'] = filepath
        if min_date is not None:
            # Re-include messages with high enough uid's.
            positive_uids = [uid for uid in retval if uid > 0]
            if positive_uids:
                min_uid = min(positive_uids)
                for uid in list(date_excludees.keys()):
                    if uid > min_uid:
                        # This message was originally excluded because of
                        # its date. It is re-included now because we want all
                        # messages with UID > min_uid.
                        retval[uid] = date_excludees[uid]
        return retval

    # Interface from BaseFolder
    def quickchanged(self, statusfolder):
        """Returns True if the Maildir has changed

        Assumes cachemessagelist() has already been called """
        # Folder has different uids than statusfolder => TRUE.
        if sorted(self.getmessageuidlist()) != \
                sorted(statusfolder.getmessageuidlist()):
            return True
        # Also check for flag changes, it's quick on a Maildir.
        for (uid, message) in list(self.getmessagelist().items()):
            if message['flags'] != statusfolder.getmessageflags(uid):
                return True
        return False  # Nope, nothing changed.

    # Interface from BaseFolder
    def msglist_item_initializer(self, uid):
        return {'flags': set(), 'filename': '/no-dir/no-such-file/'}

    # Interface from BaseFolder
    def cachemessagelist(self, min_date=None, min_uid=None):
        if self.ismessagelistempty():
            self.ui.loadmessagelist(self.repository, self)
            self.messagelist = self._scanfolder(min_date=min_date,
                                                min_uid=min_uid)
            self.ui.messagelistloaded(self.repository, self, self.getmessagecount())

    # Interface from BaseFolder
    def getmessage(self, uid):
        """Returns an email message object."""

        filename = self.messagelist[uid]['filename']
        filepath = os.path.join(self.getfullname(), filename)
        fd = open(filepath, 'rb')
        _fd_bytes = fd.read()
        fd.close()
        try: retval = self.parser['8bit'].parsebytes(_fd_bytes)
        except:
            err = exc_info()
            msg_id = self._extract_message_id(_fd_bytes)[0].decode('ascii',errors='surrogateescape')
            raise OfflineImapError(
                "Exception parsing message with ID ({}) from file ({}).\n {}: {}".format(
                    msg_id, filename, err[0].__name__, err[1]),
                OfflineImapError.ERROR.MESSAGE)
        if len(retval.defects) > 0:
            # We don't automatically apply fixes as to attempt to preserve the original message
            self.ui.warn("UID {} has defects: {}".format(uid, retval.defects))
            if any(isinstance(defect, NoBoundaryInMultipartDefect) for defect in retval.defects):
                # (Hopefully) Rare defect from a broken client where multipart boundary is
                # not properly quoted.  Attempt to solve by fixing the boundary and parsing
                self.ui.warn(" ... applying multipart boundary fix.")
                retval = self.parser['8bit'].parsebytes(self._quote_boundary_fix(_fd_bytes))
            try:
                # See if the defects after fixes are preventing us from obtaining bytes
                _ = retval.as_bytes(policy=self.policy['8bit'])
            except UnicodeEncodeError as err:
                # Unknown issue which is causing failure of as_bytes()
                msg_id = self.getmessageheader(retval, "message-id")
                if msg_id is None:
                    msg_id = '<unknown-message-id>'
                raise OfflineImapError(
                        "UID {} ({}) has defects preventing it from being processed!\n  {}: {}".format(
                            uid, msg_id, type(err).__name__, err),
                        OfflineImapError.ERROR.MESSAGE)
        return retval

    # Interface from BaseFolder
    def getmessagetime(self, uid):
        filename = self.messagelist[uid]['filename']
        filepath = os.path.join(self.getfullname(), filename)
        return os.path.getmtime(filepath)

    def new_message_filename(self, uid, flags=None, date=None):
        """Creates a new unique Maildir filename

        :param uid: The UID`None`, or a set of maildir flags
        :param flags: A set of maildir flags
        :param flags: (optional) Date
        :returns: String containing unique message filename"""

        if flags is None:
            flags = set()

        timeval, timeseq = _gettimeseq(date)
        uniq_name = '%d_%d.%d.%s,U=%d,FMD5=%s%s2,%s' % \
                    (timeval, timeseq, os.getpid(), socket.gethostname(),
                     uid, self._foldermd5, self.infosep, ''.join(sorted(flags)))
        return uniq_name.replace(os.path.sep, self.sep_subst)

    def save_to_tmp_file(self, filename, msg, policy=None):
        """Saves given message to the named temporary file in the
        'tmp' subdirectory of $CWD.

        Arguments:
        - filename: name of the temporary file;
        - msg: Email message object

        Returns: relative path to the temporary file
        that was created."""

        if policy is None:
            output_policy = self.policy['8bit']
        else:
            output_policy = policy
        tmpname = os.path.join('tmp', filename)
        # Open file and write it out.
        # XXX: why do we need to loop 7 times?
        tries = 7
        while tries:
            tries = tries - 1
            try:
                fd = os.open(os.path.join(self.getfullname(), tmpname),
                             os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o666)
                break
            except OSError as e:
                if not hasattr(e, 'EEXIST'):
                    raise
                if e.errno == errno.EEXIST:
                    if tries:
                        time.sleep(0.23)
                        continue
                    severity = OfflineImapError.ERROR.MESSAGE
                    raise OfflineImapError(
                        "Unique filename %s already exists." %
                        filename, severity,
                        exc_info()[2])
                else:
                    raise

        fd = os.fdopen(fd, 'wb')
        fd.write(msg.as_bytes(policy=output_policy))
        # Make sure the data hits the disk.
        fd.flush()
        if self.dofsync():
            os.fsync(fd)
        fd.close()

        return tmpname

    # Interface from BaseFolder
    def savemessage(self, uid, msg, flags, rtime):
        """Writes a new message, with the specified uid.

        See folder/Base for detail. Note that savemessage() does not
        check against dryrun settings, so you need to ensure that
        savemessage is never called in a dryrun mode."""

        # This function only ever saves to tmp/,
        # but it calls savemessageflags() to actually save to cur/ or new/.
        self.ui.savemessage('maildir', uid, flags, self)
        if uid < 0:
            # We cannot assign a new uid.
            return uid

        if uid in self.messagelist:
            # We already have it, just update flags.
            self.savemessageflags(uid, flags)
            return uid

        # Use the mail timestamp given by either Date or Delivery-date mail
        # headers.
        message_timestamp = None
        if self._filename_use_mail_timestamp is not False:
            try:
                message_timestamp = self.get_message_date(msg, 'Date')
                if message_timestamp is None:
                    # Give a try with Delivery-date
                    message_timestamp = self.get_message_date(
                        msg, 'Delivery-date')
            except Exception as e:
                # This should never happen.
                from offlineimap.ui import getglobalui
                datestr = self.get_message_date(msg)
                ui = getglobalui()
                ui.warn("UID %d has invalid date %s: %s\n"
                        "Not using message timestamp as file prefix" %
                        (uid, datestr, e))
                # No need to check if message_timestamp is None here since it
                # would be overridden by _gettimeseq.
        messagename = self.new_message_filename(uid, flags, date=message_timestamp)
        tmpname = self.save_to_tmp_file(messagename, msg)

        if self._utime_from_header is True:
            try:
                date = self.get_message_date(msg, 'Date')
                if date is not None:
                    os.utime(os.path.join(self.getfullname(), tmpname),
                             (date, date))
            # In case date is wrongly so far into the future as to be > max
            # int32.
            except Exception as e:
                from offlineimap.ui import getglobalui
                datestr = self.get_message_date(msg)
                ui = getglobalui()
                ui.warn("UID %d has invalid date %s: %s\n"
                        "Not changing file modification time" % (uid, datestr, e))

        self.messagelist[uid] = self.msglist_item_initializer(uid)
        self.messagelist[uid]['flags'] = flags
        self.messagelist[uid]['filename'] = tmpname
        # savemessageflags moves msg to 'cur' or 'new' as appropriate.
        self.savemessageflags(uid, flags)
        self.ui.debug('maildir', 'savemessage: returning uid %d' % uid)
        return uid

    # Interface from BaseFolder
    def getmessageflags(self, uid):
        return self.messagelist[uid]['flags']

    # Interface from BaseFolder
    def savemessageflags(self, uid, flags):
        """Sets the specified message's flags to the given set.

        This function moves the message to the cur or new subdir,
        depending on the 'S'een flag.

        Note that this function does not check against dryrun settings,
        so you need to ensure that it is never called in a
        dryrun mode."""

        assert uid in self.messagelist

        oldfilename = self.messagelist[uid]['filename']
        dir_prefix, filename = os.path.split(oldfilename)
        # If a message has been seen, it goes into 'cur'
        dir_prefix = 'cur' if 'S' in flags else 'new'

        if flags != self.messagelist[uid]['flags']:
            # Flags have actually changed, construct new filename Strip
            # off existing infostring
            infomatch = self.re_flagmatch.search(filename)
            if infomatch:
                filename = filename[:-len(infomatch.group())]  # strip off
            infostr = '%s2,%s' % (self.infosep, ''.join(sorted(flags)))
            filename += infostr

        newfilename = os.path.join(dir_prefix, filename)
        if newfilename != oldfilename:
            try:
                os.rename(os.path.join(self.getfullname(), oldfilename),
                          os.path.join(self.getfullname(), newfilename))
            except OSError as e:
                raise OfflineImapError(
                    "Can't rename file '%s' to '%s': %s" %
                    (oldfilename, newfilename, e.errno),
                    OfflineImapError.ERROR.FOLDER,
                    exc_info()[2])

            self.messagelist[uid]['flags'] = flags
            self.messagelist[uid]['filename'] = newfilename

    # Interface from BaseFolder
    def change_message_uid(self, uid, new_uid):
        """Change the message from existing uid to new_uid

        This will not update the statusfolder UID, you need to do that yourself.
        :param uid: Message UID
        :param new_uid: (optional) If given, the old UID will be changed
                        to a new UID. The Maildir backend can implement this as
                        an efficient rename.
        """

        if uid not in self.messagelist:
            raise OfflineImapError("Cannot change unknown Maildir UID %s" % uid,
                                   OfflineImapError.ERROR.MESSAGE)
        if uid == new_uid:
            return

        oldfilename = self.messagelist[uid]['filename']
        dir_prefix, filename = os.path.split(oldfilename)
        flags = self.getmessageflags(uid)
        # TODO: we aren't keeping the prefix timestamp so we don't honor the
        # filename_use_mail_timestamp configuration option.
        newfilename = os.path.join(dir_prefix,
                                   self.new_message_filename(new_uid, flags))
        os.rename(os.path.join(self.getfullname(), oldfilename),
                  os.path.join(self.getfullname(), newfilename))
        self.messagelist[new_uid] = self.messagelist[uid]
        self.messagelist[new_uid]['filename'] = newfilename
        del self.messagelist[uid]

    # Interface from BaseFolder
    def deletemessage(self, uid):
        """Unlinks a message file from the Maildir.

        :param uid: UID of a mail message
        :type uid: String
        :return: Nothing, or an Exception if UID but no corresponding file
                 found.
        """
        filename = self.messagelist[uid]['filename']
        filepath = os.path.join(self.getfullname(), filename)
        try:
            os.unlink(filepath)
        except OSError:
            # Can't find the file -- maybe already deleted?
            newmsglist = self._scanfolder()
            if uid in newmsglist:  # Nope, try new filename.
                filename = newmsglist[uid]['filename']
                filepath = os.path.join(self.getfullname(), filename)
                os.unlink(filepath)
            # Yep -- return.
        del (self.messagelist[uid])

    def migratefmd5(self, dryrun=False):
        """Migrate FMD5 hashes from versions prior to 6.3.5

        :param dryrun: Run in dry run mode
        :return: None
        """
        oldfmd5 = md5(self.name).hexdigest()
        msglist = self._scanfolder()
        for mkey, mvalue in list(msglist.items()):
            filename = os.path.join(self.getfullname(), mvalue['filename'])
            match = re.search("FMD5=([a-fA-F0-9]+)", filename)
            if match is None:
                self.ui.debug("maildir",
                              "File `%s' doesn't have an FMD5 assigned"
                              % filename)
            elif match.group(1) == oldfmd5:
                self.ui.info("Migrating file `%s' to FMD5 `%s'"
                             % (filename, self._foldermd5))
                if not dryrun:
                    newfilename = filename.replace(
                        "FMD5=" + match.group(1), "FMD5=" + self._foldermd5)
                    try:
                        os.rename(filename, newfilename)
                    except OSError as e:
                        raise OfflineImapError(
                            "Can't rename file '%s' to '%s': %s" %
                            (filename, newfilename, e.errno),
                            OfflineImapError.ERROR.FOLDER,
                            exc_info()[2])

            elif match.group(1) != self._foldermd5:
                self.ui.warn(("Inconsistent FMD5 for file `%s':"
                              " Neither `%s' nor `%s' found")
                             % (filename, oldfmd5, self._foldermd5))
