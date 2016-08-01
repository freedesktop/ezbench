"""
Copyright (c) 2015, Intel Corporation

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of Intel Corporation nor the names of its contributors
      may be used to endorse or promote products derived from this software
      without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from email.utils import parsedate_tz, mktime_tz
from collections import namedtuple
from datetime import datetime, timedelta
from dateutil import relativedelta
from array import array
from scipy import stats
from enum import Enum
from numpy import *
import statistics
import subprocess
import threading
import atexit
import pprint
import fcntl
import time
import json
import glob
import copy
import csv
import sys
import os
import re

# Import ezbench from the timings/ folder
timing_dir = os.path.abspath(sys.path[0]+"/../timing_DB/")
if not os.path.isdir(timing_dir):
    timing_dir = os.path.abspath(sys.path[0]+"/timing_DB/")
sys.path.append(timing_dir)
from timing import *

# Ezbench runs
class EzbenchExitCode(Enum):
    UNKNOWN = -1
    NO_ERROR = 0
    UNKNOWN_ERROR = 1
    CORE_ALREADY_RUNNING = 5
    REPORT_LOCKED = 6
    ARG_PROFILE_NAME_MISSING = 11
    ARG_PROFILE_INVALID = 12
    ARG_OPTARG_MISSING = 13
    ARG_REPO_MISSING = 14
    OS_SHELL_GLOBSTAT_MISSING = 30
    OS_LOG_FOLDER_CREATE_FAILED = 31
    OS_CD_REPO = 32
    GIT_INVALID_COMMIT_ID = 50
    ENV_SETUP_ERROR = 60
    COMP_DEP_UNK_ERROR = 70
    COMPILATION_FAILED = 71
    DEPLOYMENT_FAILED = 72
    DEPLOYMENT_ERROR = 73
    REBOOT_NEEDED = 74
    TEST_INVALID_NAME = 100
    UNK_ERROR = 255

class EzbenchRun:
    def __init__(self, commits, tests, avail_versions, predicted_execution_time, repo_type, repo_dir, repo_head, deployed_commit, exit_code):
        self.commits = commits
        self.tests = tests
        self.avail_versions = avail_versions
        self.predicted_execution_time = predicted_execution_time
        self.repo_type = repo_type
        self.repo_dir = repo_dir
        self.repo_head = repo_head
        self.deployed_commit = deployed_commit
        self.exit_code = EzbenchExitCode(exit_code)

    def success(self):
        return self.exit_code == EzbenchExitCode.NO_ERROR

class Ezbench:
    def __init__(self, ezbench_dir, profile = None, repo_path = None,
                 make_command = None, report_name = None, tests_folder = None,
                 run_config_script = None):
        self.ezbench_dir = ezbench_dir
        self.ezbench_path = "{}/core.sh".format(ezbench_dir)
        self.profile = profile
        self.repo_path = repo_path
        self.make_command = make_command
        self.report_name = report_name
        self.tests_folder = tests_folder
        self.run_config_script = run_config_script

        self.abortFileName = None
        if report_name is not None:
            self.abortFileName = "{}/logs/{}/requestExit".format(ezbench_dir, report_name)

    @classmethod
    def requestEarlyExit(self, ezbench_dir, report_name):
        abortFileName = "{}/logs/{}/requestExit".format(ezbench_dir, report_name)
        try:
            f = open(abortFileName, 'w')
            f.close()
            return True
        except IOError:
            return False

    def __ezbench_cmd_base(self, tests = [], test_excludes = [], rounds = None, dry_run = False, list_tests = False, list_built_versions = False):
        ezbench_cmd = []
        ezbench_cmd.append(self.ezbench_path)

        if list_tests:
            ezbench_cmd.append("-l")
            return ezbench_cmd, ""

        if list_built_versions:
            ezbench_cmd.append("-L")
            return ezbench_cmd, ""

        if self.profile is not None:
            ezbench_cmd.append("-P"); ezbench_cmd.append(self.profile)

        if self.repo_path is not None:
            ezbench_cmd.append("-p"); ezbench_cmd.append(self.repo_path)

        if len(tests) > 0:
            ezbench_cmd.append("-b"); ezbench_cmd.append("-")

        for test_excl in test_excludes:
            ezbench_cmd.append("-B"); ezbench_cmd.append(test_excl)

        if rounds is not None:
            ezbench_cmd.append("-r"); ezbench_cmd.append(str(int(rounds)))

        if self.make_command is not None:
            ezbench_cmd.append("-m"); ezbench_cmd.append(self.make_command)
        if self.report_name is not None:
            ezbench_cmd.append("-N"); ezbench_cmd.append(self.report_name)
        if self.tests_folder is not None:
            ezbench_cmd.append("-T"); ezbench_cmd.append(self.tests_folder)
        if self.run_config_script is not None:
            ezbench_cmd.append("-c"); ezbench_cmd.append(self.run_config_script)

        if dry_run:
            ezbench_cmd.append("-k")

        stdin = ""
        for test in tests:
            stdin += test + "\n"

        return ezbench_cmd, stdin

    def __run_ezbench(self, cmd, stdin, dry_run = False, verbose = False):
        exit_code = None

        if verbose:
            print(cmd); print(stdin)

        # Remove the abort file before running anything as it would result in an
        # immediate exit
        if not dry_run and self.abortFileName is not None:
            try:
                os.remove(self.abortFileName)
            except FileNotFoundError:
                pass

        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                             universal_newlines=True,
                                             input=stdin)
            exit_code = EzbenchExitCode.NO_ERROR
        except subprocess.CalledProcessError as e:
            exit_code = EzbenchExitCode(e.returncode)
            output = e.output
            pass

        # we need to parse the output
        commits= []
        tests = []
        avail_versions = []
        pred_exec_time = 0
        deployed_commit = ""
        repo_type = ""
        repo_dir = ""
        head_commit = ""
        re_commit_list = re.compile('^Testing \d+ versions: ')
        re_repo = re.compile('^Repo type = (.*), directory = (.*), version = (.*), deployed version = (.*)$')
        for line in output.split("\n"):
            m_commit_list = re_commit_list.match(line)
            m_repo = re_repo.match(line)
            if line.startswith("Tests that will be run:"):
                tests = line[24:].split(" ")
            elif line.startswith("Available tests:"):
                tests = line[17:].split(" ")
            elif line.startswith("Available versions:"):
                avail_versions = line[19:].strip().split(" ")
            elif line.find("estimated finish date:") >= 0:
                pred_exec_time = ""
            elif m_repo is not None:
                repo_type, repo_dir, head_commit, deployed_commit = m_repo.groups()
            elif m_commit_list is not None:
                commits = line[m_commit_list.end():].split(" ")
                while '' in commits:
                    commits.remove('')
            elif exit_code == EzbenchExitCode.TEST_INVALID_NAME and line.endswith("do not exist"):
                print(line)

        if len(tests) > 0 and tests[-1] == '':
            tests.pop(-1)

        if exit_code != EzbenchExitCode.NO_ERROR:
            print("\n\nERROR: The following command '{}' failed with the error code {}. Here is its output:\n\n'{}'".format(" ".join(cmd), exit_code, output))

        return EzbenchRun(commits, tests, avail_versions, pred_exec_time, repo_type, repo_dir, head_commit, deployed_commit, exit_code)

    def run(self, commits, tests, test_excludes = [],
                    rounds = None, dry_run = False, verbose = False):
        ezbench_cmd, ezbench_stdin = self.__ezbench_cmd_base(tests, test_excludes, rounds, dry_run)

        for commit in commits:
            ezbench_cmd.append(commit)

        return self.__run_ezbench(ezbench_cmd, ezbench_stdin, dry_run, verbose)

    def available_tests(self):
        ezbench_cmd, ezbench_stdin = self.__ezbench_cmd_base(list_tests = True)
        return self.__run_ezbench(ezbench_cmd, ezbench_stdin).tests

    def available_versions(self):
        ezbench_cmd, ezbench_stdin = self.__ezbench_cmd_base(list_built_versions = True)
        return self.__run_ezbench(ezbench_cmd, ezbench_stdin).avail_versions

    def reportIsLocked(self):
        if self.report_name is None:
            return False

        lockFileName = "{}/logs/{}/lock".format(self.ezbench_dir, self.report_name)

        try:
            with open(lockFileName, 'w') as lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                except IOError:
                    return True

                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception as e:
                    pass

                return False
        except Exception:
            return False
            pass


# Test sets, needed by SmartEzbench
class Testset:
    def __init__(self, filepath, name):
        self.filepath = filepath
        self.name = name
        self.description = "No description"
        self.tests = dict()

        self._ln = -1

    def __print__(self, msg, silent = False):
        if not silent:
            print("At {}:{}, {}".format(self.filepath, self._ln, msg))

    def __include_set__(self, availableTestSet, reg_exp, rounds, silent = False):
        # Convert the rounds number to integer and validate it
        try:
            rounds = int(rounds)
            if rounds < 0:
                self.__print__("the number of rounds cannot be negative ({})".format(rounds), silent)
                return False
        except ValueError:
            self.__print__("the number of rounds is invalid ({})".format(rounds), silent)
            return False

        # Now add the tests needed
        try:
            inc_re = re.compile(reg_exp)
        except Exception as e:
            self.__print__("invalid regular expression --> {}".format(e), silent)
        tests_added = 0
        for test in availableTestSet:
            if inc_re.search(test):
                self.tests[test] = rounds
                tests_added += 1

        if tests_added == 0:
            self.__print__("no tests got added", silent)
            return False
        else:
            return True

    def __exclude_set__(self, reg_exp, silent = False):
        # Now remove the tests needed
        try:
            inc_re = re.compile(reg_exp)
        except Exception as e:
            self.__print__("invalid regular expression --> {}".format(e), silent)

        to_remove = []
        for test in self.tests:
            if inc_re.search(test):
                to_remove.append(test)

        if len(to_remove) > 0:
            for entry in to_remove:
                del self.tests[entry]
        else:
            self.__print__("exclude '{}' has no effect".format(reg_exp), silent)

        return True

    def parse(self, availableTestSet, silent = False):
        try:
            with open(self.filepath) as f:
                self._ln = 1
                for line in f.readlines():
                    fields = line.split(" ")
                    if fields[0] == "description":
                        if len(fields) < 2:
                            self.__print__("description takes 1 argument", silent)
                            return False
                        self.description = " ".join(fields[1:])
                    elif fields[0] == "include":
                        if availableTestSet is None:
                            continue
                        if len(fields) != 3:
                            self.__print__("include takes 2 arguments", silent)
                            return False
                        if not self.__include_set__(availableTestSet, fields[1], fields[2], silent):
                            return False
                    elif fields[0] == "exclude":
                        if availableTestSet is None:
                            continue
                        if len(fields) != 2:
                            self.__print__("exclude takes 1 argument", silent)
                            return False
                        if not self.__exclude_set__(fields[1].strip(), silent):
                            return False
                    elif fields[0] != "\n" and fields[0][0] != "#":
                        self.__print__("invalid line", silent)
                    self._ln += 1

                return True
        except EnvironmentError:
            return False

    @classmethod
    def list(cls, ezbench_dir):
        testsets = []
        for root, dirs, files in os.walk(ezbench_dir + '/testsets.d/'):
            for f in files:
                if f.endswith(".testset"):
                    testsets.append(cls(root + f, f[0:-8]))

        return testsets

    @classmethod
    def open(cls, ezbench_dir, name):
        filename = name + ".testset"
        for root, dirs, files in os.walk(ezbench_dir + '/testsets.d/'):
            if filename in files:
                return cls(root + '/' + filename, name)
        return None

# Smart-ezbench-related classes
class Criticality(Enum):
    II = 0
    WW = 1
    EE = 2
    DD = 3

class RunningMode(Enum):
    INITIAL = 0
    RUN = 1
    PAUSE = 2
    ERROR = 3
    ABORT = 4
    RUNNING = 5

def list_smart_ezbench_report_names(ezbench_dir, updatedSince = 0):
    log_dir = ezbench_dir + '/logs'
    state_files = glob.glob("{log_dir}/*/smartezbench.state".format(log_dir=log_dir));

    reports = []
    for state_file in state_files:
        if updatedSince > 0 and os.path.getmtime(state_file) < updatedSince:
            continue

        start = len(log_dir) + 1
        stop = len(state_file) - 19
        reports.append(state_file[start:stop])

    return reports

class TaskEntry:
    def __init__(self, commit, test, rounds):
        self.commit = commit
        self.test = test
        self.rounds = rounds
        self.start_date = None
        self.exec_time = None

    def started(self):
        self.start_date = datetime.now()

    def set_timing_information(self, timingsDB):
        time = timingsDB.data("benchmark", self.test)
        if len(time) > 0:
            self.exec_time = statistics.median(time)
        else:
            self.exec_time = None

    def remaining_seconds(self):
        if self.exec_time is None:
            return None

        total = timedelta(0, self.exec_time * self.rounds)
        if self.start_date is not None:
            elapsed = timedelta(seconds=(datetime.now() - self.start_date).total_seconds())
        else:
            elapsed = timedelta(0)
        return total - elapsed

    def __str__(self):
        string = "{}: {}: {} run(s)".format(self.commit, self.test, self.rounds)

        if self.exec_time is not None:
            total_delta = timedelta(0, self.exec_time * self.rounds)

            if self.start_date is not None:
                elapsed = timedelta(seconds=(datetime.now() - self.start_date).total_seconds())
                progress = elapsed.total_seconds() * 100 / total_delta.total_seconds()

                seconds_left=timedelta(seconds=int((total_delta - elapsed).total_seconds()))
                string += "({:.2f}%, {}s remaining)".format(progress, seconds_left)
            else:
                string += "(estimated exec time: {}s)".format(timedelta(0, int(total_delta.total_seconds())))
        else:
            if self.start_date is not None:
                string += "(started {} ago)".format(datetime.now() - self.start_date)
            else:
                string += "(no estimation available)"

        return string

class SmartEzbench:
    def __init__(self, ezbench_dir, report_name, readonly = False):
        self.readonly = readonly
        self.ezbench_dir = ezbench_dir
        self.report_name = report_name
        self.log_folder = ezbench_dir + '/logs/' + report_name
        self.smart_ezbench_state = self.log_folder + "/smartezbench.state"
        self.smart_ezbench_lock = self.log_folder + "/smartezbench.lock"
        self.smart_ezbench_log = self.log_folder + "/smartezbench.log"
        self._report_cached = None

        self.state = dict()
        self.state['commits'] = dict()
        self.state['mode'] = RunningMode.INITIAL.value

        self._task_lock = threading.Lock()
        self._task_current = None
        self._task_list = None

        # Create the log directory
        first_run = False
        if not readonly and not os.path.exists(self.log_folder):
            os.makedirs(self.log_folder)
            first_run = True

        # Open the log file as append
        self.log_file = open(self.smart_ezbench_log, "a")

        # Add the welcome message
        if first_run or not self.__reload_state():
            if readonly:
                raise RuntimeError("The report {} does not exist".format(report_name))
            self.__save_state()
            self.__log(Criticality.II,
                    "Created report '{report_name}' in {log_folder}".format(report_name=report_name,
                                                                            log_folder=self.log_folder))

        # Set the state to RUN if the mode is RUNNING but it is not currently running
        if (self.state['mode'] == RunningMode.RUNNING.value and
            not Ezbench(self.ezbench_dir, report_name=self.report_name).reportIsLocked()):
                self.set_running_mode(RunningMode.RUN)


    def __log(self, error, msg):
        time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = "{time}: ({error}) {msg}\n".format(time=time, error=error.name, msg=msg)
        print(log_msg, end="")
        if not self.readonly:
            self.log_file.write(log_msg)
            self.log_file.flush()

    def __grab_lock(self):
        if self.readonly:
            return
        self.lock_fd = open(self.smart_ezbench_lock, 'w')
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
            return True
        except IOError as e:
            self.__log(Criticality.EE, "Could not lock the report: " + str(e))
            return False

    def __release_lock(self):
        if self.readonly:
            return

        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.lock_fd.close()
        except Exception as e:
            self.__log(Criticality.EE, "Cannot release the lock: " + str(e))
            pass

    def __reload_state_unlocked(self):
        # check if a report already exists
        try:
            with open(self.smart_ezbench_state, 'rt') as f:
                self.state_read_time = time.time()
                try:
                    self.state = json.loads(f.read())
                except Exception as e:
                    self.__log(Criticality.EE, "Exception while reading the state: " + str(e))
                    pass
                return True
        except IOError as e:
            self.__log(Criticality.WW, "Cannot open the state file: " + str(e))
            pass
        return False

    def __reload_state(self, keep_lock = False):
        self.__grab_lock()
        ret = self.__reload_state_unlocked()
        if not keep_lock:
            self.__release_lock()
        return ret

    def __save_state(self):
        if self.readonly:
            return

        try:
            state_tmp = str(self.smart_ezbench_state) + ".tmp"
            with open(state_tmp, 'wt') as f:
                f.write(json.dumps(self.state, sort_keys=True, indent=4, separators=(',', ': ')))
                f.close()
                os.rename(state_tmp, self.smart_ezbench_state)
                return True
        except IOError:
            self.__log(Criticality.EE, "Could not dump the current state to a file!")
            return False

    def __create_ezbench(self, ezbench_path = None, profile = None, report_name = None):
        if profile is None:
            profile = self.profile()

        return Ezbench(ezbench_dir = self.ezbench_dir, profile = profile,
                       report_name = self.report_name)

    def __read_attribute_unlocked__(self, attr, default = None):
        if attr in self.state:
            return self.state[attr]
        else:
            return default

    def __read_attribute__(self, attr, default = None):
        self.__reload_state(keep_lock=False)
        return self.__read_attribute_unlocked__(attr, default)

    def __write_attribute_unlocked__(self, attr, value, allow_updates = False):
        if allow_updates or attr not in self.state or self.state['beenRunBefore'] == False:
            self.state[attr] = value
            self.__save_state()
            return True
        return False

    def __write_attribute__(self, attr, value, allow_updates = False):
        self.__reload_state(keep_lock=True)
        ret = self.__write_attribute_unlocked__(attr, value, allow_updates)
        self.__release_lock()
        return ret

    def running_mode(self):
        return RunningMode(self.__read_attribute__('mode', RunningMode.INITIAL.value))

    def set_running_mode(self, mode):
        if mode == RunningMode.RUNNING:
            self.__log(Criticality.EE, "Ezbench running mode cannot manually be set to 'RUNNING'")
            return False

        self.__reload_state(keep_lock=True)

        # Request an early exit if we go from RUNNING to PAUSE
        cur_mode = RunningMode(self.__read_attribute_unlocked__('mode'))
        if cur_mode == RunningMode.RUNNING and mode == RunningMode.PAUSE:
            Ezbench.requestEarlyExit(self.ezbench_dir, self.report_name)

        self.__write_attribute_unlocked__('mode', mode.value, allow_updates = True)
        self.__log(Criticality.II, "Ezbench running mode set to '{mode}'".format(mode=mode.name))
        self.__release_lock()

        return True

    def profile(self):
        return self.__read_attribute__('profile')

    def set_profile(self, profile):
        self.__reload_state(keep_lock=True)
        if 'beenRunBefore' not in self.state or self.state['beenRunBefore'] == False:
            # Check that the profile exists!
            ezbench = self.__create_ezbench(profile = profile)
            run_info = ezbench.run(["HEAD"], [], [], dry_run=True)
            if not run_info.success():
                if run_info.exit_code == EzbenchExitCode.ARG_PROFILE_INVALID:
                    self.__log(Criticality.EE,
                               "Invalid profile name '{profile}'.".format(profile=profile))
                else:
                    self.__log(Criticality.EE,
                               "The following error arose '{error}'.".format(error=run_info.exit_code.name))
                self.__release_lock()
                return

            self.state['profile'] = profile
            self.__log(Criticality.II, "Ezbench profile set to '{profile}'".format(profile=profile))
            self.__save_state()
        else:
            self.__log(Criticality.EE, "You cannot change the profile of a report that already has results. Start a new one.")
        self.__release_lock()

    def conf_script(self):
        self.__reload_state(keep_lock=True)
        if "conf_script" in self.state:
            conf_script = self.state['conf_script']
            self.__release_lock()
            return conf_script
        else:
            self.__release_lock()
            return None

    def set_conf_script(self, conf_script):
        if self.__write_attribute__('conf_script', conf_script, allow_updates = False):
            self.__log(Criticality.II, "Ezbench profile configuration script set to '{0}'".format(conf_script))
        else:
            self.__log(Criticality.EE, "You cannot change the configuration script of a report that already has results. Start a new one.")

    def commit_url(self):
        return self.__read_attribute__('commit_url')

    def set_commit_url(self, commit_url):
        self.__write_attribute__('commit_url', commit_url, allow_updates = True)
        self.__log(Criticality.II, "Report commit URL has been changed to '{}'".format(commit_url))

    def __add_test_unlocked__(self, commit, test, rounds = None):
        if commit not in self.state['commits']:
            self.state['commits'][commit] = dict()
            self.state['commits'][commit]["tests"] = dict()

        if rounds is None:
            rounds = 3
        else:
            rounds = int(rounds)

        if test not in self.state['commits'][commit]['tests']:
            self.state['commits'][commit]['tests'][test] = dict()
            self.state['commits'][commit]['tests'][test]['rounds'] = rounds
        else:
            self.state['commits'][commit]['tests'][test]['rounds'] += rounds

        # if the number of rounds is equal to 0 for a test, delete it
        if self.state['commits'][commit]['tests'][test]['rounds'] <= 0:
            del self.state['commits'][commit]['tests'][test]

        # Delete a commit that has no test
        if len(self.state['commits'][commit]['tests']) == 0:
            del self.state['commits'][commit]

    def add_test(self, commit, test, rounds = None):
        self.__reload_state(keep_lock=True)
        self.__add_test_unlocked__(commit, test, rounds)
        self.__save_state()
        self.__release_lock()

    def add_testset(self, commit, testset, rounds = None):
        self.__reload_state(keep_lock=True)

        if rounds is None:
            rounds = 1
        else:
            rounds = int(rounds)

        for test in sorted(testset.tests.keys()):
            self.__add_test_unlocked__(commit, test,
                                            testset.tests[test] * rounds)

        self.__save_state()
        self.__release_lock()

    def __force_test_rounds_unlocked__(self, commit, test, at_least):
        if at_least < 1:
            return 0
        else:
            at_least = int(at_least)

        if commit not in self.state['commits']:
            self.state['commits'][commit] = dict()
            self.state['commits'][commit]["tests"] = dict()

        if test not in self.state['commits'][commit]['tests']:
            self.state['commits'][commit]['tests'][test] = dict()
            self.state['commits'][commit]['tests'][test]['rounds'] = 0

        to_add = at_least - self.state['commits'][commit]['tests'][test]['rounds']

        if to_add > 0:
            self.__log(Criticality.WW,
                       "Schedule {} more runs for the test {} on commit {}".format(to_add, test, commit))

            self.state['commits'][commit]['tests'][test]['rounds'] += to_add

        if to_add > 0:
            return to_add
        else:
            return 0

    def force_test_rounds(self, commit, test, at_least):
        self.__reload_state(keep_lock=True)
        ret = self.__force_test_rounds_unlocked__(commit, test, at_least)
        self.__save_state()
        self.__release_lock()

        return ret

    def task_info(self):
        self._task_lock.acquire()
        tl = copy.deepcopy(self._task_list)
        c = copy.deepcopy(self._task_current)
        self._task_lock.release()

        db = TimingsDB(self.ezbench_dir + "/timing_DB")

        if c is not None:
            c.set_timing_information(db)
        if tl is not None:
            for t in tl:
                t.set_timing_information(db)
        return c, tl

    def __prioritize_runs(self, task_tree, deployed_version):
        task_list = list()

        # Aggregate all the subtests
        for commit in task_tree:
            test_subtests = dict()
            test_rounds = dict()

            # First, read all the tests and aggregate them
            for test in task_tree[commit]["tests"]:
                basename, subtests = Test.parse_name(test)
                if basename not in test_subtests:
                    test_subtests[basename] = set()
                test_subtests[basename] |= set(subtests)
                test_rounds[basename] = max(test_rounds.get(basename, 0),
                                       task_tree[commit]["tests"][test]["rounds"])

            # Destroy the state before reconstructing it!
            task_tree[commit]["tests"] = dict()
            for basename in test_subtests:
                full_name = Test.partial_name(basename, list(test_subtests[basename]))
                task_tree[commit]["tests"][full_name] = dict()
                task_tree[commit]["tests"][full_name]["rounds"] = test_rounds[basename]

        # Schedule the tests using the already-deployed version
        if deployed_version is not None and deployed_version in task_tree:
            for test in task_tree[deployed_version]["tests"]:
                rounds = task_tree[deployed_version]["tests"][test]["rounds"]
                task_list.append(TaskEntry(deployed_version, test, rounds))
            del task_tree[deployed_version]

        # Add all the remaining tasks in whatever order!
        for commit in task_tree:
            for test in task_tree[commit]["tests"]:
                rounds = task_tree[commit]["tests"][test]["rounds"]
                task_list.append(TaskEntry(commit, test, rounds))

        return task_list

    def __change_state_to_run__(self):
        self.__reload_state(keep_lock=True)
        ret = False
        running_state=RunningMode(self.__read_attribute_unlocked__('mode', RunningMode.INITIAL.value))
        if running_state == RunningMode.INITIAL or running_state == RunningMode.RUNNING:
            self.__write_attribute_unlocked__('mode', RunningMode.RUN.value, allow_updates = True)
            self.__log(Criticality.II, "Ezbench running mode set to RUN")
            ret = True
        elif running_state != RunningMode.RUN:
            self.__log(Criticality.II,
                       "We cannot run when the current running mode is {mode}.".format(mode=running_state.name))
            ret = False
        else:
            ret = True
        self.__release_lock()
        return ret

    def __change_state_to_running__(self):
        self.__reload_state(keep_lock=True)
        ret = False
        running_state=RunningMode(self.__read_attribute_unlocked__('mode', RunningMode.INITIAL.value))
        if running_state == RunningMode.INITIAL or running_state == RunningMode.RUN:
            self.__write_attribute_unlocked__('mode', RunningMode.RUNNING.value, allow_updates = True)
            self.__log(Criticality.II, "Ezbench running mode set to RUNNING")
            ret = True
        else:
            self.__log(Criticality.II,
                       "We cannot run when the current running mode is {mode}.".format(mode=running_state.name))
            ret = False
        self.__release_lock()
        return ret

    def __done_running__(self):
        self.__reload_state(keep_lock=True)
        running_state=RunningMode(self.__read_attribute_unlocked__('mode'))
        if running_state == RunningMode.RUNNING or running_state == RunningMode.RUN:
            self.__write_attribute_unlocked__('mode', RunningMode.RUN.value, allow_updates = True)

        self._task_current = None
        self._task_list = None
        self._task_lock.release()

    def __remove_task_from_tasktree__(self, task_tree, commit, full_name, rounds):
        if commit.sha1 not in task_tree:
            return False
        if full_name not in task_tree[commit.sha1]["tests"]:
            return False

        task_tree[commit.sha1]["tests"][full_name]['rounds'] -= rounds

        if task_tree[commit.sha1]["tests"][full_name]['rounds'] <= 0:
            del task_tree[commit.sha1]["tests"][full_name]

        if len(task_tree[commit.sha1]["tests"]) == 0:
            del task_tree[commit.sha1]

        return True

    def run(self):
        self.__log(Criticality.II, "----------------------")
        self.__log(Criticality.II, "Starting a run: {report} ({path})".format(report=self.report_name, path=self.log_folder))

        # Change state to RUN or fail if we are not in the right mode
        if not self.__change_state_to_run__():
            return False

        self.__log(Criticality.II, "Checking the dependencies:")

        # check for dependencies
        if 'profile' not in self.state:
            self.__log(Criticality.EE, "    - Ezbench profile: Not set. Abort...")
            return False
        else:
            profile = self.state["profile"]
            self.__log(Criticality.II, "    - Ezbench profile: '{0}'".format(profile))

        # Create the ezbench runner
        ezbench = self.__create_ezbench()
        run_info = ezbench.run(["HEAD"], [], [], dry_run=True)
        self.__log(Criticality.II, "    - Deployed version: '{0}'".format(run_info.deployed_commit))
        self.__log(Criticality.II, "All the dependencies are met, generate a report...")

        # Generate a report to compare the goal with the current state
        report = Report(self.log_folder, silentMode = True)
        self.__log(Criticality.II,
                   "The report contains {count} commits".format(count=len(report.commits)))

        # Walk down the report and get rid of every run that has already been made!
        task_tree = copy.deepcopy(self.state['commits'])
        for commit in report.commits:
            for result in commit.results:
                self.__log(Criticality.DD,
                           "Found {count} runs for test {test} using commit {commit}".format(count=len(result.result()),
                                                                                                       commit=commit.sha1,
                                                                                                       test=result.test.full_name))

                for key in result.results():
                    full_name = Test.partial_name(result.test.full_name, [key])
                    self.__remove_task_from_tasktree__(task_tree, commit, full_name, len(result.result(key)))

        # Delete the tests on commits that do not compile
        for commit in report.commits:
            if commit.build_broken() and commit.sha1 in task_tree:
                self.__log(Criticality.II,
                           "Cancelling the following runs because commit {} does not compile:".format(commit.sha1))
                self.__log(Criticality.II, task_tree[commit.sha1])
                del task_tree[commit.sha1]

        if len(task_tree) == 0:
            self.__log(Criticality.II, "Nothing left to do, exit")
            return False

        task_tree_str = pprint.pformat(task_tree)
        self.__log(Criticality.II, "Task list: {tsk_str}".format(tsk_str=task_tree_str))

        # Let's start!
        if not self.__change_state_to_running__():
            return False
        self.state['beenRunBefore'] = True

        # Prioritize --> return a list of commits to do in order
        self._task_lock.acquire()
        self._task_list = self.__prioritize_runs(task_tree, run_info.deployed_commit)

        # Start generating ezbench calls
        while len(self._task_list) > 0:
            running_mode = self.running_mode()
            if running_mode != RunningMode.RUNNING:
                self.__log(Criticality.II,
                       "Running mode changed from RUNNING to {mode}. Exit...".format(mode=running_mode.name))
                self.__done_running__()
                return False

            self._task_current = e = self._task_list.pop(0)
            short_name=e.test[:80].rsplit('|', 1)[0]+'...'
            self.__log(Criticality.DD,
                       "make {count} runs for test {test} using commit {commit}".format(count=e.rounds,
                                                                                                  commit=e.commit,
                                                                                                  test=short_name))
            self._task_current.started()
            self._task_lock.release()
            run_info = ezbench.run([e.commit], [e.test + '$'], rounds=e.rounds)
            self._task_lock.acquire()

            if run_info.success():
                continue

            # We got an error, let's see what we can do about it!
            if run_info.exit_code.value < 40:
                # Error we cannot do anything about, probably a setup issue
                # Let's mark the run as aborted until the user resets it!
                self.set_running_mode(RunningMode.ERROR)
            elif (run_info.exit_code == EzbenchExitCode.COMPILATION_FAILED or
                  run_info.exit_code == EzbenchExitCode.DEPLOYMENT_FAILED):
                # Cancel any other test on this commit
                self._task_list = [x for x in self._task_list if not x.commit == e.commit]

        self._task_current = None

        self.__done_running__()
        self.__log(Criticality.II, "Done")

        return True

    def git_history(self):
        git_history = list()

        # Get the repo directory
        ezbench = self.__create_ezbench()
        run_info = ezbench.run(["HEAD"], [], [], dry_run=True)

        if not run_info.success() or run_info.repo_dir == '':
            return git_history

        # Get the list of commits and store their position in the list in a dict
        output = subprocess.check_output(["/usr/bin/git", "log", "--first-parent", "--format=%h %ct"],
                                          cwd=run_info.repo_dir).decode().split('\n')

        GitCommit = namedtuple('GitCommit', 'sha1 timestamp')
        for line in output:
            fields = line.split(' ')
            if len(fields) == 2:
                git_history.append(GitCommit(fields[0], fields[1]))

        return git_history

    def report(self, git_history=list(), reorder_commits = True,
               cached_only = False, restrict_to_commits = []):
        if cached_only:
            return self._report_cached

        if reorder_commits and len(git_history) == 0:
            git_history = self.git_history()

        # Generate the report, order commits based on the git history
        r = Report(self.log_folder, silentMode = True,
                                 restrict_to_commits = restrict_to_commits)
        r.enhance_report([c.sha1 for c in git_history])
        return r

    def __find_middle_commit__(self, git_history, old, new):
        if not hasattr(self, "__find_middle_commit__cache"):
            self.__find_middle_commit__cache = dict()

        key = "{}->{}".format(old, new)
        if key in self.__find_middle_commit__cache:
            return self.__find_middle_commit__cache[key]

        old_idx = git_history.index(old)
        new_idx = git_history.index(new)
        middle_idx = int(old_idx - ((old_idx - new_idx) / 2))
        if middle_idx != old_idx and middle_idx != new_idx:
            middle = git_history[middle_idx]
        else:
            middle = None

        self.__find_middle_commit__cache[key] = middle
        return middle

    # WARNING: test may be None!
    def __score_event__(self, git_history, commit_sha1, test, severity):
        commit_weight = 1 - (git_history.index(commit_sha1) / len(git_history))

        test_weight = 1
        if test is not None and hasattr(test, 'score_weight'):
            test_weight = test.score_weight

        return commit_weight * test_weight * severity

    def schedule_enhancements(self, git_history=None, max_variance = 0.025,
                              perf_diff_confidence = 0.95, smallest_perf_change=0.005,
                              max_run_count = 100, commit_schedule_max = 1):
        self.__log(Criticality.II, "Start enhancing the report")

        # Generate the report, order commits based on the git history
        if git_history is None:
            git_history = self.git_history()
        commits_rev_order = [c.sha1 for c in git_history]
        r = Report(self.log_folder, silentMode = True)
        r.enhance_report(commits_rev_order, max_variance, perf_diff_confidence,
                         smallest_perf_change)

        # FIXME: Have a proper tracking of state changes to say if this cache
        # is up to date or not. This could be used later to avoid parsing the
        # report every time.
        self._report_cached = r

        # Check all events
        tasks = []
        for e in r.events:
            commit_sha1 = None
            test = None
            event_prio = 1
            severity = 0 # should be a value in [0, 1]
            test_name_to_run = ""
            runs = 0
            if type(e) is EventBuildBroken:
                if e.commit_range.old is None or e.commit_range.is_single_commit():
                    continue
                middle = self.__find_middle_commit__(commits_rev_order,
                                                     e.commit_range.old.sha1,
                                                     e.commit_range.new.sha1)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                severity = 1
                event_prio = 0.5
                test_name_to_run = "no-op"
                runs = 1
            elif type(e) is EventBuildFixed:
                if e.fixed_commit_range.is_single_commit():
                    continue
                middle = self.__find_middle_commit__(commits_rev_order,
                                                     e.fixed_commit_range.old.sha1,
                                                     e.fixed_commit_range.new.sha1)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                severity = 1
                event_prio = 0.5
                test_name_to_run = "no-op"
                runs = 1
            elif type(e) is EventPerfChange:
                if e.commit_range.is_single_commit():
                    continue

                # ignore commits which have a big variance
                result_new = r.find_result(e.commit_range.new, e.test).result()
                if result_new.margin() > max_variance:
                    continue
                result_old = r.find_result(e.commit_range.old, e.test).result()
                if result_old.margin() > max_variance:
                    continue

                middle = self.__find_middle_commit__(commits_rev_order,
                                                     e.commit_range.old.sha1,
                                                     e.commit_range.new.sha1)
                if middle is None:
                    continue

                # FIXME: handle the case where the middle commit refuses to build

                # Schedule the work
                commit_sha1 = middle
                test = e.test
                severity = min(abs(e.diff()), 1) * e.confidence
                event_prio = 0.75

                test_name_to_run = test.full_name
                runs = (len(result_old) + len(result_new)) / 2
            elif isinstance(e, EventResultNeedsMoreRuns):
                commit_sha1 = e.result.commit.sha1
                missing_runs = max(1, e.wanted_n() - len(e.result)) # Schedule at least 1 more runs
                severity = min(missing_runs / len(e.result), 1)
                event_prio = 1

                test_name_to_run = e.result.subtest_fullname()
                additional_runs = min(20, missing_runs) # cap the maximum amount of runs to play nice

                # Make sure we do not schedule more than the maximum amount of run
                runs = len(e.result) + additional_runs
                if runs > max_run_count:
                    runs = max_run_count - len(e.result)
                    if runs == 0:
                        continue
            elif type(e) is EventUnitResultChange:
                if e.commit_range.is_single_commit():
                    continue

                # Find the middle commit
                middle = self.__find_middle_commit__(commits_rev_order,
                                                     e.commit_range.old.sha1,
                                                     e.commit_range.new.sha1)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                severity = 1
                event_prio = 1
                test_name_to_run = str(e.subtest_name)
                runs = math.ceil((len(e.old_result) + len(e.new_result)) / 2)
            else:
                print("schedule_enhancements: unknown event type {}".format(type(e).__name__))
                continue

            score = self.__score_event__(commits_rev_order, commit_sha1, test, severity)
            score *= event_prio

            tasks.append((score, commit_sha1, test_name_to_run, runs, e))

        # If we are using the throttle mode, only schedule the commit with the
        # biggest score to speed up bisecting of the most important issues
        tasks_sorted = sorted(tasks, key=lambda t: t[0])
        scheduled_commits = added = 0
        self.__reload_state(keep_lock=True)
        while len(tasks_sorted) > 0 and scheduled_commits < commit_schedule_max:
            commit = tasks_sorted[-1][1]
            self.__log(Criticality.DD, "Add all the tasks using commit {}".format(commit))
            added = 0
            for t in tasks_sorted:
                if t[1] == commit:
                    added += self.__force_test_rounds_unlocked__(t[1], t[2], t[3])
            if added > 0:
                self.__log(Criticality.II, "{}".format(t[4]))
                scheduled_commits += 1
            else:
                self.__log(Criticality.DD, "No work scheduled using commit {}, try another one".format(commit))
            del tasks_sorted[-1]
        if added > 0:
            self.__save_state()
        self.__release_lock()

        self.__log(Criticality.II, "Done enhancing the report")

# Report parsing
class Test:
    def __init__(self, full_name, unit="undefined"):
        self.full_name = full_name
        self.prevValue = -1
        self.unit_str = unit

    # returns (base_name, subtests=[])
    @classmethod
    def parse_name(cls, full_name):
        idx = full_name.find('[')
        if idx > 0:
            if full_name[-1] != ']':
                print("WARNING: test name '{}' is invalid.".format(full_name))

            basename = full_name[0 : idx]
            subtests = full_name[idx + 1 : -1].split('|')
        else:
            basename = full_name
            subtests = []

        return (basename, subtests)

    @classmethod
    def partial_name(self, basename, sub_tests):
        name = basename
        if len(sub_tests) > 0 and len(sub_tests[0]) > 0:
            name += "["
            for i in range(0, len(sub_tests)):
                if i != 0:
                    name += "|"
                name += sub_tests[i]
            name += "]"
        return name

class ListStats:
    def __init__(self, data):
        self.data = data

        # cached data
        self._cache_result = None
        self._cache_mean = None
        self._cache_std = None
        self._cache_mean_simple = None

    def invalidate_cache(self):
        """ Trash the cache, necessary if you manually update the data (BAD!) """

        self._cache_result = None
        self._cache_mean = None
        self._cache_std = None

    def __samples_needed__(self, sigma, margin, confidence=0.95):
        # TODO: Find the function in scipy to get these values
        if confidence <= 0.9:
            z = 1.645
        elif confidence <= 0.95:
            z = 1.960
        else:
            z = 2.576
        return ((z * sigma) / margin)**2

    def __compute_stats__(self):
        if self._cache_mean is None or self._cache_std is None:
            if len(self.data) > 1:
                self._cache_mean, var, self._cache_std = stats.bayes_mvs(array(self.data),
                                                                         alpha=0.95)
                if math.isnan(self._cache_mean[0]):
                    self._cache_mean = (self.data[0], (self.data[0], self.data[0]))
                    self._cache_std = (0, (0, 0))
            else:
                if len(self.data) == 0:
                    value = 0
                else:
                    value = self.data[0]
                self._cache_mean = (value, (value, value))
                self._cache_std = (float("inf"), (float("inf"), float("inf")))

    def margin(self):
        """ Computes the margin of error for the sample set """

        self.__compute_stats__()
        if self._cache_mean[0] > 0:
            return (self._cache_mean[1][1] - self._cache_mean[1][0]) / 2 / self._cache_mean[0]
        else:
            return 0

    # wanted_margin is a number between 0 and 1
    def confidence_margin(self, wanted_margin = None, confidence=0.95):
        """
        Computes the confidence margin of error and how many samples would be
        needed to guarantee that mean of the data would be inside the wanted_margin.

        Args:
            wanted_margin: A float that represents how much variance you accept. For example, 0.025 would mean the accepted variance is 2.5%
            confidence: The wanted confidence level

        Returns:
            the current margin of error
            the number of samples needed to reach the wanted confidence
        """

        data = array(self.data)
        if len(data) < 2 or data.var() == 0:
            return 0, 2

        self.__compute_stats__()
        margin = self.margin()
        wanted_samples = 2

        if wanted_margin is not None:
            # TODO: Get sigma from the test instead!
            sigma = (self._cache_std[1][1] - self._cache_std[1][0]) / 2
            target_margin = self._cache_mean[0] * wanted_margin
            wanted_samples = math.ceil(self.__samples_needed__(sigma,
                                                               target_margin,
                                                               confidence))

        return margin, wanted_samples

    def mean(self):
        """ Computes the mean of the data set """

        if self._cache_mean is not None:
            return self._cache_mean[0]

        if self._cache_mean_simple is None:
            if len(self.data) > 0:
                self._cache_mean_simple = sum(self.data) / len(self.data)
            else:
                self._cache_mean_simple = 0
        return self._cache_mean_simple

    def compare(self, distrib_b, equal_var=True):
        """
        Compare the current sample distribution to distrib_b's

        Args:
            distrib_b: the distribution to compare to
            equal_var: f True (default), perform a standard independent 2 sample test that assumes equal population variances [R263]. If False, perform Welch’s t-test, which does not assume equal population variance [R264].

        Returns:
            the difference of the means (self - b)
            the confidence of them being from the same normal distribution
        """

        t, p = stats.ttest_ind(distrib_b.data, self.data,
                               equal_var = equal_var)
        if distrib_b.mean() > 0:
            diff = abs(self.mean() - distrib_b.mean()) / distrib_b.mean()
        else:
            diff = float('inf')

        return diff, 1 - p

    def __len__(self):
        return len(self.data)

class BenchSubTestType(Enum):
    SUBTEST_FLOAT = 0
    SUBTEST_STRING = 1
    SUBTEST_IMAGE = 2
    METRIC = 3

class SubTestBase:
    def __init__(self, name, subtestType, averageValue, unit = None, data_raw_file = None):
        self.name = name
        self.subtest_type = subtestType
        self.value = averageValue
        self.unit = unit
        self.data_raw_file = data_raw_file

class SubTestString(SubTestBase):
    def __init__(self, name, value, data_raw_file = None):
        super().__init__(name, BenchSubTestType.SUBTEST_STRING, value, None, data_raw_file)

class SubTestFloat(SubTestBase):
    def __init__(self, name, unit, samples, data_raw_file = None):
        self.samples = ListStats(samples)

        super().__init__(name, BenchSubTestType.SUBTEST_FLOAT, self.samples.mean(), unit, data_raw_file)

class Metric(SubTestFloat):
    def __init__(self, name, unit, samples, timestamps = None, data_raw_file = None):
        super().__init__(name, unit, samples, data_raw_file)
        self.subtest_type = BenchSubTestType.METRIC
        self.timestamps = timestamps

    def exec_time(self):
        """
        Returns the difference between the last and the first timestamp or 0 if
        there are no timestamps.
        """
        if self.timestamps is not None and len(self.timestamps) > 0:
            return self.timestamps[-1]
        else:
            return 0

class TestRun:
    def __init__(self, testResult, testType, runFile, metricsFiles, mainValueType = None, mainValue = None):
        self.test_result = testResult
        self.run_file = runFile
        self.main_value_type = mainValueType
        self.main_value = mainValue

        self._results = dict()

        # Add the environment file
        self.env_file = runFile + ".env_dump"
        if not os.path.isfile(self.env_file):
            self.env_file = None

        if testType == "bench":
            # There are no subtests here
            data, unit, more_is_better = readCsv(runFile)
            if len(data) > 0:
                result = SubTestFloat("", testResult.unit_str, data, runFile)
                self.__add_result__(result)
        elif testType == "unit":
            unit_tests = readUnitRun(runFile)
            for subtest in unit_tests:
                result = SubTestString(subtest, unit_tests[subtest], runFile)
                self.__add_result__(result)
        else:
            raise ValueError("Ignoring results because the type '{}' is unknown".format(testType))

        for f in metricsFiles:
            self.__add_metrics__(f)

    def __add_result__(self, subtest):
        if subtest.subtest_type == BenchSubTestType.METRIC:
            key = "metric_{}".format(subtest.name)
        else:
            key = subtest.name

        # Verify that the subtest does not already exist
        if key in self._results:
            msg = "Raw data file '{}' tries to add an already-existing result '{}' (found in '{}')"
            msg = msg.format(subtest.data_raw_file, key, self._results[key].data_raw_file)
            raise ValueError(msg)

        self._results[key] = subtest

    def __add_metrics__(self, metric_file):
        values = dict()
        with open(metric_file, 'rt') as f:

            # Do not try to open files bigger than 1MB
            if os.fstat(f.fileno()).st_size > 1e6:
                raise ValueError('The metric file is too big (> 1MB)')

            reader = csv.DictReader(f)
            try:
                # Collect stats about each metrics
                for row in reader:
                    if row is None or len(row) == 0:
                        continue

                    # Verify that all the fields are present or abort...
                    allValuesOK = True
                    for field in values:
                        if row[field] is None:
                            allValuesOK = False
                            break
                    if not allValuesOK:
                        break

                    for field in row:
                        if field not in values:
                            values[field] = list()
                        values[field].append(float(row[field]))
            except csv.Error as e:
                sys.stderr.write('file %s, line %d: %s\n' % (filepath, reader.line_num, e))
                return

        # Find the time values and store them aside after converting them to seconds
        time_unit_re = re.compile(r'^time \((.+)\)$')
        time = list()
        for field in values:
            m = time_unit_re.match(field)
            if m is not None:
                unit = m.groups()[0]
                factor = 1
                if unit == "s":
                    factor = 1
                elif unit == "ms":
                    factor = 1e-3
                elif unit == "us" or unit == "µs":
                    factor = 1e-6
                elif unit == "ns":
                    factor = 1e-9
                else:
                    print("unknown time unit '{}'".format(unit))
                for v in values[field]:
                    time.append(v * factor)

        # Create the metrics
        metric_name_re = re.compile(r'^(.+) \((.+)\)$')
        for field in values:
            unit = None
            m = metric_name_re.match(field)
            if m is not None:
                metric_name, unit = m.groups()
            else:
                metric_name = field

            if metric_name.lower() == "time":
                continue

            vals = list()
            timestamps = list()
            for v in range(0, len(values[field])):
                vals.append(values[field][v])
                timestamps.append(time[v] - time[0])
            metric = Metric(metric_name, unit, vals, timestamps, metric_file)
            self.__add_result__(metric)

            # Try to add more metrics by combining them
            if unit == "W" or unit == "J":
                power_value = None
                if unit == "W":
                    if metric.exec_time() > 0:
                        energy_name = metric_name + ":energy"
                        power_value =  metric.samples.mean()
                        value = power_value * metric.exec_time()
                        energy_metric = Metric(energy_name, "J", [value], [metric.exec_time()], metric_file)
                        self.__add_result__(energy_metric)
                elif unit == "J":
                    if metric.exec_time() > 0:
                        energy_name = metric_name + ":power"
                        power_value = metric.samples.mean() / metric.exec_time()
                        power_metric = Metric(energy_name, "W", [power_value], [metric.exec_time()], metric_file)
                        self.__add_result__(power_metric)

                if power_value is not None and self.main_value_type == "FPS":
                    efficiency_name = metric_name + ":efficiency"
                    value = self.main_value / power_value
                    unit = "{}/W".format(self.main_value_type)
                    efficiency_metric = Metric(efficiency_name, unit, [value], [metric.exec_time()], metric_file)
                    self.__add_result__(efficiency_metric)

    def result(self, key = None):
        """ Returns the result associated to the key or None if it does not exist """
        if key is None:
            return SubTestFloat(None, self.main_value_type, [self.main_value], self.test_result.test_file)
        if key in self._results:
            return self._results[key]
        else:
            return None

    def results(self, restrict_to_type = None):
        """
        Returns a set of all the available keys for results (to be queried
        individually using the result() method). You may select only one type
        of results by using the restrict_to_type parameter.

        Args:
            restrict_to_type: A BenchSubTestType to only list the results of a certain type

        """
        if restrict_to_type is None:
            return self._results.keys()
        else:
            return set([x for x in self._results if self._results[x].subtest_type == restrict_to_type])


class SubTestResult:
    def __init__(self, commit, test, key, runs):
        self.commit = commit
        self.test = test
        self.key = key
        self.runs = runs

        self.value_type = None
        self.unit = None
        self.results = []

        for run in runs:
            run_result = run.result(key)
            if run_result is None:
                continue
            if self.value_type is None:
                self.value_type = run_result.subtest_type
            elif self.value_type != run_result.subtest_type:
                msg ="Tried to add a result (run file '{}') for the subtest '{}' with type {} to list only containing the type {}"
                msg = msg.format(run_result.data_raw_file, subtest,
                                 run_result.subtest_type, self.value_type)
                raise ValueError(msg)
            if self.unit is None:
                self.unit = run_result.unit
            elif self.unit != run_result.unit:
                msg ="Tried to add a result (run file '{}') for the subtest '{}' with unit '{}' to list only containing the unit '{}'"
                msg = msg.format(run_result.data_raw_file, subtest,
                                 run_result.unit, self.unit)
                raise ValueError(msg)

            # Do not add empty samples
            if (run_result.value is None or
                ((self.value_type == BenchSubTestType.SUBTEST_FLOAT or
                     self.value_type == BenchSubTestType.METRIC)
                and math.isnan(run_result.value))):
                continue
            self.results.append((run, run_result.value))

        self._cache_list = None
        self._cache_list_stats = None

    def __len__(self):
        return len(self.to_list())

    def __getitem__(self, key):
        return self.to_list()[key]

    def to_list(self):
        """ Returns the list of all the mean values for every run """

        if self._cache_list is None:
            self._cache_list = [x[1] for x in self.results if x[1] is not None]
        return self._cache_list

    def to_set(self):
        """ Returns a set of all the values found at every run """

        return set(self.to_list())

    def to_liststat(self):
        """ Convenience method that returns a ListStats(self.to_list()) object """

        if self._cache_list_stats is None:
            if (self.value_type == BenchSubTestType.SUBTEST_FLOAT or
                self.value_type == BenchSubTestType.METRIC):
                self._cache_list_stats = ListStats(self.to_list())
            else:
                self._cache_list_stats = ListStats([])
        return self._cache_list_stats

    def compare(self, old_subtestresult):
        """
        Compare the current sample distribution to old_subtestresult's

        Args:
            old_subtestresult: the subrest to compare to
            equal_var: f True (default), perform a standard independent 2 sample test that assumes equal population variances [R263]. If False, perform Welch’s t-test, which does not assume equal population variance [R264].

        Returns:
            the difference of the means (self - b)
            the confidence of them being from the same normal distribution

        WARNING: Does not work when the type is SUBTEST_STRING
        """

        return self.to_liststat().compare(old_subtestresult.to_liststat())

    def mean(self):
        """
        Returns the mean of the values. Only works on numbers outputs.

        WARNING: Does not work when the type is SUBTEST_STRING
        """
        return self.to_liststat().mean()

    def margin(self):
        """
        Computes the margin of error for the sample set.

        WARNING: Does not work when the type is SUBTEST_STRING
        """
        return self.to_liststat().margin()

    def confidence_margin(self, wanted_margin = None, confidence=0.95):
        """
        Computes the confidence margin of error and how many samples would be
        needed to guarantee that mean of the data would be inside the wanted_margin.

        Args:
            wanted_margin: A float that represents how much variance you accept. For example, 0.025 would mean the accepted variance is 2.5%
            confidence: The wanted confidence level

        Returns:
            the current margin of error
            the number of samples needed to reach the wanted confidence

        WARNING: Does not work when the type is SUBTEST_STRING
        """

        liststat = self.to_liststat()
        return self.to_liststat().confidence_margin(wanted_margin, confidence)

    def subtest_fullname(self):
        """
        Returns the name that should be used to reproduce this result.

        WARNING: Does not work when the type is SUBTEST_STRING
        """
        if self.value_type != BenchSubTestType.METRIC:
            return Test.partial_name(self.test.full_name, [self.key])
        else:
            return None

class TestResult:
    def __init__(self, commit, test, testType, testFile, runFiles, metricsFiles):
        self.commit = commit
        self.test = test
        self.test_file = testFile

        self.runs = []
        self.test_type = testType
        self.more_is_better = True
        self.unit_str = None

        self._results = set()
        self._cache_result = None

        self.__parse_results__(testType, testFile, runFiles, metricsFiles)

    def __parse_results__(self, testType, testFile, runFiles, metricsFiles):
        # Read the data and abort if there is no data
        data, unit_str, self.more_is_better = readCsv(testFile)
        if len(data) == 0:
            raise ValueError("The TestResult {} does not contain any runs".format(testFile))

        if unit_str is None:
            unit_str = "FPS"
        self.unit_str = unit_str

        # Check that we have the same unit as the test
        if self.test.unit_str != self.unit_str:
            if self.test.unit_str != "undefined":
                msg = "The unit used by the test '{test}' changed from '{unit_old}' to '{unit_new}' in commit {commit}"
                print(msg.format(test=test.full_name,
                                unit_old=test.unit_str,
                                unit_new=self.unit_str,
                                commit=commit.sha1))
            self.test.unit_str = unit_str

        for i in range(0, len(runFiles)):
            run = TestRun(self, testType, runFiles[i], metricsFiles[runFiles[i]], unit_str, data[i])
            self._results |= run.results()
            self.runs.append(run)


    def result(self, key = None):
        """ Returns the result associated to the key or None if it does not exist """

        if self._cache_result is None:
            self._cache_result = dict()
        if key not in self._cache_result:
            if len(self.runs) == 0:
                raise ValueError('Cannot get the results when there are no runs ({})'.format(self.test_file))
            self._cache_result[key] = SubTestResult(self.commit, self.test, key, self.runs)
        return self._cache_result[key]

    def results(self, restrict_to_type = None):
        """
        Returns a set of all the available keys for results (to be queried
        individually using the result() method). You may select only one type
        of results by using the restrict_to_type parameter.

        Args:
            restrict_to_type: A BenchSubTestType to only list the results of a certain type

        """

        if restrict_to_type is None:
            return self._results
        else:
            res = set()
            for run in self.runs:
                res |= run.results(restrict_to_type)
            return res


class Commit:
    def __init__(self, sha1, full_name, label):
        self.sha1 = sha1
        self.full_name = full_name
        self.label = label

        self.results = []
        self.geom_mean_cache = -1

        self.__parse_commit_information__()

    def __parse_commit_information__(self):
        self.compile_log = self.sha1 + "_compile_log"
        self.patch = self.sha1 + ".patch"

        # Set default values then parse the patch
        self.full_sha1 = self.sha1
        self.author = "UNKNOWN AUTHOR"
        self.commiter = "UNKNOWN COMMITER"
        self.author_date = datetime.min
        self.commit_date = datetime.min
        self.title = ''
        self.commit_log = ''
        self.signed_of_by = set()
        self.reviewed_by = set()
        self.tested_by = set()
        self.bugs = set()
        try:
            with open(self.patch, 'r') as f:
                log_started = False
                fdo_bug_re = re.compile('fdo#(\d+)')
                basefdourl = "https://bugs.freedesktop.org/show_bug.cgi?id="
                for line in f:
                    line = line.strip()
                    if line == "---": # Detect the end of the header
                        break
                    elif line.startswith('commit'):
                        self.full_sha1 = line.split(' ')[1]
                    elif line.startswith('Author:'):
                        self.author = line[12:]
                    elif line.startswith('AuthorDate: '):
                        self.author_date = datetime.fromtimestamp(mktime_tz(parsedate_tz(line[12:])))
                    elif line.startswith('Commit:'):
                        self.commiter = line[12:]
                    elif line.startswith('CommitDate: '):
                        self.commit_date = datetime.fromtimestamp(mktime_tz(parsedate_tz(line[12:])))
                    elif line == '':
                        # The commit log is about to start
                        log_started = True
                    elif log_started:
                        if self.title == '':
                            self.title = line
                        else:
                            self.commit_log += line + '\n'
                            if line.startswith('Reviewed-by: '):
                                self.reviewed_by |= {line[13:]}
                            elif line.startswith('Signed-off-by: '):
                                self.signed_of_by |= {line[15:]}
                            elif line.startswith('Tested-by: '):
                                self.tested_by |= {line[11:]}
                            elif line.startswith('Bugzilla: '):
                                self.bugs |= {line[10:]}
                            elif line.startswith('Fixes: '):
                                self.bugs |= {line[7:]}
                            else:
                                fdo_bug_m = fdo_bug_re.search(line)
                                if fdo_bug_m is not None:
                                    bugid = fdo_bug_m.groups()[0]
                                    self.bugs |= {basefdourl + bugid}
        except Exception:
            self.patch = None
            pass

        # Look for the exit code
        self.compil_exit_code = EzbenchExitCode.UNKNOWN
        try:
            with open(self.compile_log, 'r') as f:
                for line in f:
                    pass
                # Line contains the last line of the report, parse it
                if line.startswith("Exiting with error code "):
                    self.compil_exit_code = EzbenchExitCode(int(line[24:]))
        except Exception:
            self.compile_log = None
            pass

    def build_broken(self):
        return (self.compil_exit_code.value >= EzbenchExitCode.COMP_DEP_UNK_ERROR.value and
                self.compil_exit_code.value <= EzbenchExitCode.DEPLOYMENT_ERROR.value)

    def geom_mean(self):
        """ Returns the geometric mean of the average performance of all the
        tests (default key).
        """

        if self.geom_mean_cache >= 0:
            return self.geom_mean_cache

        # compute the variance
        s = 1
        n = 0
        for result in self.results:
            testresults = result.result()
            if len(testresults) > 0:
                s *= testresults.mean()
                n = n + 1
        if n > 0:
            value = s ** (1 / n)
        else:
            value = 0

        geom_mean_cache = value
        return value

class EventCommitRange:
    def __init__(self, old, new = None):
        self.old = old
        if new is None:
            self.new = old
        else:
            self.new = new

    def is_single_commit(self):
        return self.distance() <= 1

    def distance(self):
        if self.old is not None:
            if hasattr(self.old, "git_distance_head") and hasattr(self.new, "git_distance_head"):
                return self.old.git_distance_head - self.new.git_distance_head
            else:
                return -1
        else:
            return sys.maxsize

    def __str__(self):
        if self.new == None:
            return "commit {}".format(self.old.sha1)

        if self.is_single_commit():
            return "commit {}".format(self.new.sha1)
        elif self.old is not None:
            distance = self.distance()
            if distance == -1:
                distance = "unkown"
            return "commit range {}:{}({})".format(self.old.sha1, self.new.sha1,
                                                   distance)
        else:
            return "commit before {}".format(self.new.sha1)


        float("inf")

class EventBuildBroken:
    def __init__(self, commit_range):
        self.commit_range = commit_range

    def __str__(self):
        return "{} broke the build".format(self.commit_range)

class EventBuildFixed:
    def __init__(self, broken_commit_range, fixed_commit_range):
        self.broken_commit_range = broken_commit_range
        self.fixed_commit_range = fixed_commit_range

    def broken_for_time(self):
        if (self.broken_commit_range.new.commit_date > datetime.min and
            self.fixed_commit_range.old.commit_date > datetime.min):
            attrs = ['years', 'months', 'days', 'hours', 'minutes', 'seconds']
            human_readable = lambda delta: ['%d %s' % (getattr(delta, attr),
                                                       getattr(delta, attr) > 1 and attr or attr[:-1])
                for attr in attrs if getattr(delta, attr)]
            res=', '.join(human_readable(relativedelta.relativedelta(self.fixed_commit_range.old.commit_date,
                                                                     self.broken_commit_range.new.commit_date)))
            if len(res) == 0:
                return "0 seconds"
            else:
                return res
        return None

    def broken_for_commit_count(self):
        if (hasattr(self.broken_commit_range.new, "git_distance_head") and
            hasattr(self.fixed_commit_range.new, "git_distance_head")):
            return (self.broken_commit_range.new.git_distance_head -
                    self.fixed_commit_range.new.git_distance_head)
        else:
            return -1

    def __str__(self):
        parenthesis = ""
        if (not self.broken_commit_range.is_single_commit() or
            not self.fixed_commit_range.is_single_commit()):
            parenthesis = "at least "
        parenthesis += "after "

        time = self.broken_for_time()
        if time is not None and time != "":
            parenthesis += time + " and "
        commits = self.broken_for_commit_count()
        if commits == -1:
            commits = "unknown"
        parenthesis += "{} commits".format(commits)

        main = "{} fixed the build regression introduced by {}"
        main = main.format(self.fixed_commit_range, self.broken_commit_range)
        return "{} ({})".format(main, parenthesis)

class EventPerfChange:
    def __init__(self, test, commit_range, old_perf, new_perf, confidence):
        self.test = test
        self.commit_range = commit_range
        self.old_perf = old_perf
        self.new_perf = new_perf
        self.confidence = confidence

    def diff(self):
        if self.old_perf != 0:
            return (1 - (self.new_perf / self.old_perf)) * -1
        elif self.new_perf == 0 and self.old_perf == 0:
            return 0
        else:
            return float("inf")

    def __str__(self):
        msg = "{} changed the performance of {} from {:.2f} to {:.2f} ({:+.2f}%) with confidence p={:.2f}"
        return msg.format(self.commit_range, self.test.full_name,
                          self.old_perf, self.new_perf, self.diff() * 100,
                          self.confidence)

class EventResultNeedsMoreRuns:
    def __init__(self, result, wanted_n):
        self.result = result
        self._wanted_n = wanted_n

    def wanted_n(self):
        return self._wanted_n

    def __str__(self):
        msg = "Result {} on commit {} requires at least {} runs."
        return msg.format(self.result.subtest_fullname(), self.result.commit.sha1,
                          self.wanted_n())

class EventInsufficientSignificance(EventResultNeedsMoreRuns):
    def __init__(self, result, wanted_margin):
        super().__init__(result, result.confidence_margin(wanted_margin)[1])
        self.wanted_margin = wanted_margin

    def margin(self):
        return self.result.confidence_margin(self.wanted_margin)[0]

    def __str__(self):
        msg = "Test {} on commit {} requires more runs to reach the wanted margin ({:.2f}% vs {:.2f}%), proposes n = {}."
        return msg.format(self.result.test.full_name, self.result.commit.sha1,
                          self.margin() * 100, self.wanted_margin * 100, self.wanted_n())

class EventUnitResultChange:
    def __init__(self, subtest_name, commit_range, old_result, new_result):
        self.subtest_name = subtest_name
        self.commit_range = commit_range
        self.old_result = old_result
        self.new_result = new_result
        self.old_status = old_result[0]
        self.new_status = new_result[0]

    def __str__(self):
        msg = "{} changed the status of {} from {} to {}"
        return msg.format(self.commit_range, self.subtest_name,
                          self.old_status, self.new_status)

class EventUnitResultUnstable:
    def __init__(self, result):
        self.result = result

    def __str__(self):
        msg = "Unstable result on version {} for {} (got {})"
        return msg.format(self.result.commit.sha1, self.result.subtest_fullname(),
                          self.result.to_set())

class Report:
    def __init__(self, log_folder, silentMode = False, restrict_to_commits = []):
        self.log_folder = log_folder
        self.name = log_folder.split(os.sep)[-1]

        self.tests = list()
        self.commits = list()
        self.notes = list()
        self.events = list()

        self.__parse_report__(silentMode, restrict_to_commits)

    def __readNotes__(self):
        try:
            with open("notes", 'rt') as f:
                return f.readlines()
        except:
            return []

    def __readCommitLabels__(self):
        labels = dict()
        try:
            f = open( "commit_labels", "r")
            try:
                labelLines = f.readlines()
            finally:
                f.close()
        except IOError:
            return labels

        for labelLine in labelLines:
            fields = labelLine.split(" ")
            sha1 = fields[0]
            label = fields[1].split("\n")[0]
            labels[sha1] = label

        return labels

    def __parse_report__(self, silentMode, restrict_to_commits):
        # Save the current working directory and switch to the log folder
        cwd = os.getcwd()
        os.chdir(self.log_folder)

        # Look for the commit_list file
        try:
            f = open( "commit_list", "r")
            try:
                commitsLines = f.readlines()
            finally:
                f.close()
        except IOError:
            if not silentMode:
                sys.stderr.write("The log folder '{0}' does not contain a commit_list file\n".format(self.log_folder))
            return False

        # Read all the commits' labels
        labels = self.__readCommitLabels__()

        # Check that there are commits
        if (len(commitsLines) == 0):
            if not silentMode:
                sys.stderr.write("The commit_list file is empty\n")
            return False

        # Find all the result files and sort them by sha1
        files_list = os.listdir()
        testFiles = dict()
        commit_test_file_re = re.compile(r'^(.+)_(bench|unit)_[^\.]+(.metrics_[^\.]+)?$')
        for f in files_list:
            if os.path.isdir(f):
                continue
            m = commit_test_file_re.match(f)
            if m is not None:
                sha1 = m.groups()[0]
                if sha1 not in testFiles:
                    testFiles[sha1] = []
                testFiles[sha1].append((f, m.groups()[1]))
        files_list = None

        # Gather all the information from the commits
        if not silentMode:
            print ("Reading the results for {0} commits".format(len(commitsLines)))
        commits_txt = ""
        table_entries_txt = ""
        for commitLine in commitsLines:
            full_name = commitLine.strip(' \t\n\r')
            sha1 = commitLine.split()[0]

            label = labels.get(sha1, sha1)
            if (len(restrict_to_commits) > 0 and sha1 not in restrict_to_commits
                and label not in restrict_to_commits):
                continue
            commit = Commit(sha1, full_name, label)

            # Add the commit to the list of commits
            commit.results = sorted(commit.results, key=lambda res: res.test.full_name)
            self.commits.append(commit)

            # If there are no results, just continue
            if sha1 not in testFiles:
                continue

            # find all the tests
            for testFile, testType in testFiles[sha1]:
                # Skip when the file is a run file (finishes by #XX)
                if re.search(r'#\d+$', testFile) is not None:
                    continue

                # Skip on unrelated files
                if "." in testFile:
                    continue

                # Get the test name
                test_name = testFile[len(commit.sha1) + len(testType) + 2:]

                # Find the right Test or create one if none are found
                try:
                    test = next(b for b in self.tests if b.full_name == test_name)
                except StopIteration:
                    test = Test(test_name)
                    self.tests.append(test)

                # Look for the runs
                run_re = re.compile(r'^{testFile}#[0-9]+$'.format(testFile=testFile))
                runsFiles = [f for f,t in testFiles[sha1] if run_re.search(f)]
                runsFiles.sort(key=lambda x: '{0:0>100}'.format(x).lower()) # Sort the runs in natural order

                # Look for metrics!
                metricsFiles = dict()
                for runFile in runsFiles:
                    metricsFiles[runFile] = list()
                    metrics_re = re.compile(r'^{}.metrics_.+$'.format(runFile))
                    for metric_file in [f for f,t in testFiles[sha1] if metrics_re.search(f)]:
                        metricsFiles[runFile].append(metric_file)

                # Create the result object
                try:
                    result = TestResult(commit, test, testType, testFile, runsFiles, metricsFiles)

                    # Add the result to the commit's results
                    commit.results.append(result)
                    commit.compil_exit_code = EzbenchExitCode.NO_ERROR # The deployment must have been successful if there is data
                except:
                    pass

        # Sort the list of tests
        self.tests = sorted(self.tests, key=lambda test: test.full_name)

        # Read the notes before going back to the original folder
        notes = self.__readNotes__()

        # Go back to the original folder
        os.chdir(cwd)

    def find_commit_by_id(self, sha1):
        for commit in self.commits:
            if commit.sha1 == sha1:
                return commit
        return None

    def find_result(self, commit, test):
        for result in commit.results:
            if result.test == test:
                return result
        return None

    def enhance_report(self, commits_rev_order, max_variance = 0.025,
                       perf_diff_confidence = 0.95, smallest_perf_change=0.005):
        if len(commits_rev_order) > 0:
            # Get rid of the commits that are not in the commits list
            to_del = list()
            for c in range(0, len(self.commits)):
                if self.commits[c].sha1 not in commits_rev_order:
                    to_del.append(c)
            for v in reversed(to_del):
                del self.commits[v]

            # Add the index inside the commit
            for commit in self.commits:
                commit.git_distance_head = commits_rev_order.index(commit.sha1)

            # Sort the remaining commits
            self.commits.sort(key=lambda commit: len(commits_rev_order) - commit.git_distance_head)

        # Generate events
        commit_prev = None
        test_prev = dict()
        unittest_prev = dict()
        build_broken_since = None
        for commit in self.commits:
            commit_range = EventCommitRange(commit_prev, commit)

            # Look for compilation errors
            if commit.build_broken() and build_broken_since is None:
                self.events.append(EventBuildBroken(commit_range))
                build_broken_since = EventCommitRange(commit_prev, commit)
            elif not commit.build_broken() and build_broken_since is not None:
                self.events.append(EventBuildFixed(build_broken_since, commit_range))
                build_broken_since = None

            # Look for interesting events
            for testresult in commit.results:
                for result_key in testresult.results():
                    result = testresult.result(result_key)
                    test = result.test.full_name
                    test_unit = result.test.unit_str

                    if result.value_type == BenchSubTestType.SUBTEST_FLOAT:
                        if result.margin() > max_variance:
                            self.events.append(EventInsufficientSignificance(result, max_variance))

                        # All the other events require a git history which we do not have, continue...
                        if len(commits_rev_order) == 0:
                            continue

                        if test in test_prev:
                            # We got previous perf results, compare!
                            old_perf = test_prev[test]
                            diff, confidence = result.compare(old_perf)

                            # If we are not $perf_diff_confidence sure that this is the
                            # same normal distribution, say that the performance changed
                            if confidence >= perf_diff_confidence and diff >= smallest_perf_change:
                                commit_range = EventCommitRange(test_prev[test].commit, commit)
                                self.events.append(EventPerfChange(result.test,
                                                                commit_range,
                                                                old_perf.mean(),
                                                                result.mean(),
                                                                confidence))
                        test_prev[test] = result
                    elif result.value_type == BenchSubTestType.SUBTEST_STRING:
                        subtest_name = result.subtest_fullname()

                        # Verify that all the samples are the same
                        if len(result.to_set()) > 1:
                            self.events.append(EventUnitResultUnstable(result))

                            # Reset the state of the test and continue
                            unittest_prev[subtest_name] = None
                            continue

                        # All the other events require a git history, so skip
                        # them if we do not have it!
                        if len(commits_rev_order) == 0:
                            continue

                        # Check for differences with the previous commit
                        # NOTE: unittest_prev and result now can only contain one
                        # element due to the test above
                        if subtest_name in unittest_prev:
                            before = unittest_prev[subtest_name]
                            if before[0] != result[0]:
                                if len(before) < 2:
                                    self.events.append(EventResultNeedsMoreRuns(before, 2))
                                if len(result) < 2:
                                    self.events.append(EventResultNeedsMoreRuns(result, 2))
                                if len(before) >= 2 and len(result) >= 2:
                                    commit_range = EventCommitRange(unittest_prev[subtest_name].commit, commit)
                                    self.events.append(EventUnitResultChange(subtest_name,
                                                                                commit_range,
                                                                                before,
                                                                                result))

                        unittest_prev[subtest_name] = result

                    elif result.value_type == BenchSubTestType.METRIC:
                        # Nothing to do for now, until we start bisecting
                        # power.
                        pass
                    else:
                        print("WARNING: enhance_report: unknown result type {}".format(result.value_type))

            commit_prev = commit

def readCsv(filepath):
    data = []

    h1 = re.compile('^# (.*) of \'(.*)\' using commit (.*)$')
    h2 = re.compile('^# (.*) \\((.*) is better\\) of \'(.*)\' using (commit|version) (.*)$')

    with open(filepath, 'rt') as f:
        reader = csv.reader(f)
        unit = None
        more_is_better = True
        try:
            for row in reader:
                if row is None or len(row) == 0:
                    continue

                # try to extract information from the header
                m1 = h1.match(row[0])
                m2 = h2.match(row[0])
                if m2 is not None:
                    # groups: unit type, more|less qualifier, test, commit/version, commit_sha1
                    unit = m2.groups()[0]
                    more_is_better = m2.groups()[1].lower() == "more"
                elif m1 is not None:
                    # groups: unit type, test, commit_sha1
                    unit = m1.groups()[0]

                # Read the actual data
                if len(row) > 0 and not row[0].startswith("# "):
                    try:
                        data.append(float(row[0]))
                    except ValueError as e:
                        sys.stderr.write('Error in file %s, line %d: %s\n' % (filepath, reader.line_num, e))
        except csv.Error as e:
            sys.stderr.write('file %s, line %d: %s\n' % (filepath, reader.line_num, e))
            return [], "none"

    return data, unit, more_is_better

def readUnitRun(filepath):
    tests = dict()
    with open(filepath, 'rt') as f:
        for line in f.readlines():
            fields = line.split(':')
            if len(fields) == 2:
                tests[fields[0]] = fields[1].strip()
    return tests

def getPerformanceResultsCommitTest(commit, test):
    for result in commit.results:
        if result.test != test:
            continue

        return array(result.data)

    return array([])

def getResultsTestDiffs(commits, test):
    results = []

    # Compute a report per application
    i = 0
    origValue = -1
    for commit in commits:
        resultFound = False
        for result in commit.results:
            if result.test != test:
                continue

            value = array(result.data).mean()
            if origValue > -1:
                diff = (value * 100.0 / origValue) - 100.0
            else:
                origValue = value
                diff = 0

            results.append([i, diff])
            resultFound = True

        if not resultFound:
            results.append([i, NaN])
        i = i + 1

    return results

def getResultsGeomDiffs(commits):
    results = []

    # Compute a report per application
    i = 0
    origValue = -1
    for commit in commits:
        value = commit.geom_mean()
        if origValue > -1:
            diff = (value * 100.0 / origValue) - 100.0
        else:
            origValue = value
            diff = 0

        results.append([i, diff])
        i = i + 1

    return results

def convert_unit(value, input_unit, output_unit):
	ir_fps = -1.0

	if input_unit == output_unit:
		return value

	if input_unit.lower() == "fps":
		ir_fps = value
	elif value == 0:
		return 0

	if input_unit == "s":
		ir_fps = 1.0 / value
	elif input_unit == "ms":
		ir_fps = 1.0e3 / value
	elif input_unit == "us" or output_unit == "µs":
		ir_fps = 1.0e6 / value

	if ir_fps == -1:
		print("convert_unit: Unknown input type " + input_unit)
		return value

	if output_unit.lower() == "fps":
		return ir_fps
	elif ir_fps == 0:
		return float('+inf')

	if output_unit == "s":
		return 1.0 / ir_fps
	elif output_unit == "ms":
		return 1.0e3 / ir_fps
	elif output_unit == "us" or output_unit == "µs":
		return 1.0e6 / ir_fps

	print("convert_unit: Unknown output type " + output_unit)
	return value

def compute_perf_difference(unit, target, value):
    if unit == "s" or unit == "ms" or unit == "us" or unit == "µs" or unit == "J" or unit == "W":
        if value != 0:
            return target * 100.0 / value
        else:
            return 100
    else:
        if target != 0:
            return value * 100.0 / target
        else:
            return 100
