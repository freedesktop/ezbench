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

from collections import namedtuple, deque
from datetime import datetime, timedelta
from enum import Enum
import numpy as np
import multiprocessing
import statistics
import subprocess
import threading
import shutil
import pprint
import fcntl
import time
import json
import glob
import copy
import math
import sys
import gc
import os
import re

ezbench_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.append(os.path.join(ezbench_dir, 'timing_DB'))

from ezbench.testset import *
from ezbench.report import *
from ezbench.runner import *
from timing import *

# Smart-ezbench-related classes
class Criticality(Enum):
    DD = 4
    II = 3
    WW = 2
    EE = 1

class RunningMode(Enum):
    INITIAL = 0
    RUN = 1
    PAUSE = 2
    ERROR = 3
    ABORT = 4
    DONE = 5
    TIMEOUT = 6

    # Intermediate steps, going from RUN to RUNNING or RUNNING to PAUSE/ABORT
    INTERMEDIATE = 100
    RUNNING = 101
    PAUSING = 102
    ABORTING = 103

class TaskEntry:
    def __init__(self, commit, test, rounds, resumeResultFile = None,
                 user_requested = True):
        self.commit = commit
        self.test = test
        self.rounds = rounds
        self.resumeResultFile = resumeResultFile
        self.start_date = None
        self.exec_time = None
        self.build_time = None
        self.user_requested = user_requested

        self.cur_round = 1
        self.last_round_completed_date = None

    def started(self):
        self.start_date = datetime.now()

    def round_done(self):
        self.cur_round += 1
        self.last_round_completed_date = datetime.now()

    def predicted_completion_time(self):
        if self.last_round_completed_date is None:
            b = 0
            if self.build_time is not None:
                b = self.build_time

            e = 0
            if self.exec_time is not None:
                e = self.exec_time

            return timedelta(0, b + e)
        else:
            # Use the current information to guess the exec time
            time_diff = self.last_round_completed_date - self.start_date
            avg_time_per_round = time_diff / (self.cur_round - 1)

            return avg_time_per_round * self.rounds

    def set_timing_information(self, timingsDB, compilation_time = None,
                               available_versions = {}):
        if compilation_time is not None and self.commit not in available_versions:
            self.build_time = compilation_time

        time = timingsDB.data("test", self.test)
        if len(time) > 0:
            self.exec_time = statistics.median(time) * self.rounds
        else:
            self.exec_time = None

    def remaining_time(self):
        if self.start_date is not None:
            elapsed = datetime.now() - self.start_date
        else:
            elapsed = timedelta(0)
        return self.predicted_completion_time() - elapsed

    def __str__(self):
        if self.resumeResultFile is None:
            if self.start_date is not None:
                runs_str = "run [{}/{}]".format(self.cur_round, self.rounds)
            else:
                runs_str = "{} run(s)".format(self.rounds)

            string = "{}: {}: {} ".format(self.commit, self.test, runs_str)

        else:
            string = "resume {}".format(self.resumeResultFile)

        total_delta = self.predicted_completion_time()
        if total_delta.total_seconds() > 0:
            remaining = self.remaining_time()

            if self.start_date is not None:
                progress = 100.0 - (remaining.total_seconds() * 100 / total_delta.total_seconds())

                if remaining.total_seconds() > 0:
                    remaining_str = str(timedelta(0, math.ceil(remaining.total_seconds()))) + "s remaining"
                else:
                    remaining_str = str(timedelta(0, math.floor(-remaining.total_seconds()))) + "s overtime"

                if self.resumeResultFile is None:
                    string += "({:.2f}%, {})".format(progress, remaining_str)
                else:
                    string += "(> {:.2f}%, up to {})".format(progress, remaining_str)
            else:
                rounded_total_delta = timedelta(0, math.ceil(total_delta.total_seconds()))
                string += "(estimated completion time: {}s)".format(rounded_total_delta)
        else:
            if self.start_date is not None:
                time = datetime.now() - self.start_date
                rounded_time = timedelta(0, math.ceil(time.total_seconds()))
                string += "(started {} ago)".format(rounded_time)
            else:
                string += "(no estimation available)"

        if not self.user_requested:
            string += " (autogenerated)"

        return string

class SmartEzbenchAttributes(Enum):
    event_min_confidence = 1

    schedule_max_commits = 100

    perf_min_change = 200

    variance_max = 300
    variance_max_run_count = 301
    variance_min_run_count = 302

    report_priority = 400
    report_deadline_soft = 401
    report_deadline_hard = 402

class StateLock:
    def __init__(self, lock_path):
        self.lock_path = lock_path
        self.rlock = threading.RLock()
        self.lock_fd = None

        # Since rlock does not allow us to query the count
        self._ref_cnt = 0
        self._ref_cnt_lock = threading.Lock()

    def __del__(self):
        with self._ref_cnt_lock:
            if self._ref_cnt > 0:
                print("WARNING: StateLock instance is destroyed with ref_cnt > 0")

    def ref_cnt(self):
        with self._ref_cnt_lock:
            return self._ref_cnt

    def acquire(self):
        # First, make sure to acquire the process lock, that prevents multiple
        # threads from doing concurent accesses
        self.rlock.acquire()

        # Keep track of the count the lock has been acquired
        with self._ref_cnt_lock:
            self._ref_cnt += 1

            if self._ref_cnt == 1:
                try:
                    self.lock_fd = open(self.lock_path, 'w')
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
                    return True
                except IOError as e:
                    print("Could not lock the report: " + str(e))
                    self._ref_cnt = 0
                    self.rlock.release()
                    raise ValueError("Can't lock the report")

    def release(self):
        # Keep track of the count the lock has been acquired
        with self._ref_cnt_lock:
            self._ref_cnt -= 1

            if self._ref_cnt == 0:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                    self.lock_fd.close()
                    self.lock_fd = None
                except Exception as e:
                    print("Cannot release the lock: " + str(e))
                    pass

        # Finally, release the process lock, that prevents multiple threads from
        # doing concurent accesses
        self.rlock.release()

class SmartEzbench:
    state_locks = dict()
    state_locks_lock = threading.Lock()

    @classmethod
    def list_reports(cls, ezbench_dir, updatedSince = 0):
        log_dir = ezbench_dir + '/logs'
        state_files = glob.glob("{}/**/smartezbench.state".format(log_dir), recursive=True)

        reports = []
        for state_file in state_files:
            if updatedSince > 0 and os.path.getmtime(state_file) < updatedSince:
                continue

            start = len(log_dir) + 1
            stop = len(state_file) - 19
            reports.append(state_file[start:stop])

        return reports

    def __init__(self, ezbench_dir, report_name, readonly = False,
                 hook_binary_path = None, logs_callback = None,
                 hooks_callback = None):
        self.readonly = readonly
        self.ezbench_dir = ezbench_dir
        self.report_name = report_name
        self.log_folder = os.path.abspath(ezbench_dir + '/logs/' + report_name)
        self.hook_binary_path = hook_binary_path
        self.logs_callback = logs_callback
        self.hooks_callback = hooks_callback
        self.smart_ezbench_state = self.log_folder + "/smartezbench.state"
        self.smart_ezbench_lock = self.log_folder + "/smartezbench.lock"
        self.smart_ezbench_log = self.log_folder + "/smartezbench.log"
        self._report_cached = None
        self._first_run = False
        self._deleted = False

        # Make sure we do not use two different instances of StateLock for the
        # same report
        with self.state_locks_lock:
            self.state_lock = self.state_locks.get(report_name, None)
            if self.state_lock is None:
                self.state_locks[report_name] = StateLock(self.smart_ezbench_lock)
                self.state_lock = self.state_locks[report_name]

        self.state = dict()
        self.state['commits'] = dict()
        self.state['mode'] = RunningMode.INITIAL.value

        self._task_lock = threading.Lock()
        self._task_current = None
        self._task_list = None
        self._events_str = None

        self.min_criticality = Criticality.II

        # Verify that the absolute path actually starts with ezbench_dir + '/logs/'
        base_path = os.path.abspath(ezbench_dir + '/logs/')
        if not self.log_folder.startswith(base_path):
            raise ValueError("Invalid report name")

        # Create the log directory
        if not readonly and not os.path.exists(self.log_folder):
            os.makedirs(self.log_folder)
            self._first_run = True

        # Open the log file as append
        self.log_file = open(self.smart_ezbench_log, "a")

        # Add the welcome message
        if self._first_run or not self.__reload_state():
            if readonly:
                raise RuntimeError("The report {} does not exist".format(report_name))
            self.__save_state()
            self.__log(Criticality.II,
                    "Created report '{report_name}' in {log_folder}".format(report_name=report_name,
                                                                            log_folder=self.log_folder))

    def delete(self):
        self.__grab_lock()

        try:
            if not shutil.rmtree.avoids_symlink_attacks:
                self.__log(Criticality.WW, "Deleting the report unsafely (symlink attack)")
            else:
                self.__log(Criticality.II, "Deleting the report({}) safely".format(self.log_folder))
            shutil.rmtree(self.log_folder)

            self._deleted = True
        finally:
            self.__release_lock()

    def __log(self, error, msg):
        time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = "{time}: ({error}) {msg}\n".format(time=time, error=error.name, msg=msg)
        if error.value <= self.min_criticality.value:
            print(log_msg, end="")
            if self.logs_callback is not None:
                self.logs_callback(self, log_msg)
            if not self.readonly:
                self.log_file.write(log_msg)
                self.log_file.flush()

    # WARNING: Call this function after making sure state has an up-to-date state
    def __call_hook__(self, action, parameters = dict()):
        if self.hooks_callback is not None:
            HookCallbackState = namedtuple('HookCallbackState', ['sbench', 'action',
                                                                 'hook_parameters'])
            state = HookCallbackState(sbench=self, action=str(action),
                                      hook_parameters=parameters)
            try:
                self.hooks_callback(state)
            except:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.write("\n")

        if self.hook_binary_path is None:
            return

        env = os.environ.copy()
        env["action"] = str(action)
        env["ezbench_dir"] = str(self.ezbench_dir)
        env["ezbench_report_name"] = str(self.report_name)
        env["ezbench_report_path"] = str(self.log_folder)
        env["ezbench_report_mode"] = str(RunningMode(self.state['mode']).name)
        for p in parameters:
            env[p] = str(parameters[p])

        proc = subprocess.Popen([self.hook_binary_path], env=env)
        proc.wait()

    def __grab_lock(self):
        if self.readonly:
            return
        else:
            self.state_lock.acquire()

    def __release_lock(self):
        if self.readonly:
            return
        else:
            self.state_lock.release()

    def __update_state_version__(self):
        upgraded = False
        if self.state.get("version", 0) == 0:
            self.__log(Criticality.II, "state: v0 -> v1: add a version parameter")
            self.state['version'] = 1
            upgraded = True

        if self.state.get("version", 0) == 1:
            self.__log(Criticality.II, "state: v1 -> v2: move commits to tasks->user")
            self.state['version'] = 2

            # Create a new hierarchie for tasks
            self.state['tasks'] = dict()
            self.state['tasks']['user'] = dict()
            self.state['tasks']['user']['commits'] = self.state['commits']
            del self.state['commits']
            upgraded = True

        if self.state.get("version", 0) == 2:
            self.__log(Criticality.II, "state: v2 -> v3: create a new 'auto' sections for tasks")
            self.state['version'] = 3
            self.state['tasks']['auto'] = dict()
            self.state['tasks']['auto']['commits'] = dict()
            upgraded = True

        if self.state.get("version", 0) == 3:
            self.__log(Criticality.II, "state: v3 -> v4: create a new 'user_data' section")
            self.state['version'] = 4
            self.state['user_data'] = dict()
            upgraded = True

        latest_version = 4
        if self.state.get("version", 0) > latest_version:
            msg = "The state's version is higher than the latest supported version: {} vs {}"
            raise ValueError(msg.format(self.state.get("version", 0), latest_version))

        if upgraded:
            self.__save_state()

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

                self.__update_state_version__()
                return True
        except IOError as e:
            self.__log(Criticality.WW, "Cannot open the state file: " + str(e))
            pass
        return False

    def __reload_state(self, keep_lock=False):
        self.__grab_lock()

        if self.state_lock.ref_cnt() == 1 or self.readonly:
            ret = self.__reload_state_unlocked()
        else:
            print("WARNING: Recursive call to __reload_state()")
            ret = True

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

    def __create_ezbench(self, ezbench_path = None, profile = None):
        """
        WARNING: The state mutex must be taken!
        """
        runner = Runner(self.ezbench_dir)
        runner.set_report_name(self.report_name)

        if profile is None:
            profile = self.__read_attribute_unlocked__('profile')
            if profile is None:
                return None
        runner.set_profile(profile)

        for conf_script in self.__read_attribute_unlocked__('conf_scripts', []):
            runner.add_conf_script(conf_script)

        return runner

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
        ret = False
        self.__reload_state(keep_lock=True)
        try:
            ret = self.__write_attribute_unlocked__(attr, value, allow_updates)
        finally:
            self.__release_lock()
        return ret

    def __running_mode_unlocked__(self, check_running = True):
        mode = self.__read_attribute_unlocked__('mode', RunningMode.INITIAL.value) % RunningMode.INTERMEDIATE.value

        if check_running and Ezbench(self.ezbench_dir, report_name=self.report_name).reportIsLocked():
            mode += RunningMode.INTERMEDIATE.value

        return RunningMode(mode)

    def running_mode(self, check_running = True):
        ret = RunningMode.INITIAL
        self.__reload_state(keep_lock=True)
        try:
            ret = self.__running_mode_unlocked__(check_running)
        finally:
            self.__release_lock()
        return ret

    def __set_running_mode_unlocked__(self, mode):
        # Request an early exit if we go from RUNNING to PAUSE or
        dsk_mode = self.__running_mode_unlocked__(check_running = False)
        cur_mode = self.__running_mode_unlocked__()
        if cur_mode.value > RunningMode.INTERMEDIATE.value and mode != RunningMode.RUN:
            Ezbench.requestEarlyExit(self.ezbench_dir, self.report_name)

        self.__write_attribute_unlocked__('mode', mode.value, allow_updates = True)

        if mode != dsk_mode:
            self.__log(Criticality.II, "Ezbench running mode set to '{mode}'".format(mode=mode.name))

            params = dict()
            params['ezbench_report_mode_prev'] = dsk_mode.name
            self.__call_hook__('mode_changed', params)

    def set_running_mode(self, mode):
        if mode.value >= RunningMode.INTERMEDIATE.value:
            self.__log(Criticality.EE, "Ezbench mode cannot manually be set to '{}'".format(mode.name))
            return False

        self.__reload_state(keep_lock=True)
        try:
            self.__set_running_mode_unlocked__(mode)
        finally:
            self.__release_lock()

        return True

    # Return True if the reported got created by this instance of SmartEzbench
    def first_run(self):
        return self._first_run

    def profile(self):
        return self.__read_attribute__('profile')

    def set_profile(self, profile):
        ret = False
        self.__reload_state(keep_lock=True)
        try:
            if 'beenRunBefore' not in self.state or self.state['beenRunBefore'] == False:
                # Check that the profile exists!
                try:
                    runner = self.__create_ezbench(profile=profile)

                    self.state['profile'] = profile
                    self.__log(Criticality.II, "Ezbench profile set to '{profile}'".format(profile=profile))
                    self.__save_state()
                    ret = True
                except RunnerError as e:
                    if e.args[0]['err_code'] == RunnerErrorCode.CMD_PROFILE_INVALID:
                        self.__log(Criticality.EE,
                                "Invalid profile name '{}'.".format(profile))
                    else:
                        self.__log(Criticality.EE,
                                "The following error arose '{}({})'.".format(e.args[0]['err_code'],
                                                                                e.args[0]['err_str']))
            elif profile == self.state['profile']:
                ret = True
            else:
                self.__log(Criticality.EE, "You cannot change the profile of a report that already has results. Start a new one.")
        finally:
            self.__release_lock()

        return ret

    def conf_scripts(self):
        return self.__read_attribute__('conf_scripts', [])

    def add_conf_script(self, conf_script):
        self.__reload_state(keep_lock=True)
        try:
            if 'beenRunBefore' not in self.state or self.state['beenRunBefore'] == False:
                if "conf_scripts" not in self.state:
                    self.state['conf_scripts'] = list()

                if conf_script not in self.state['conf_scripts']:
                    self.__log(Criticality.II, "Add configuration script '{0}'".format(conf_script))
                    self.state['conf_scripts'].append(conf_script)
                    self.__save_state()
            else:
                self.__log(Criticality.EE, "You cannot change the set of scripts of a report that already has results. Start a new one.")
        finally:
            self.__release_lock()

    def remove_conf_script(self, conf_script):
        self.__reload_state(keep_lock=True)
        try:
            if 'beenRunBefore' not in self.state or self.state['beenRunBefore'] == False:
                if "conf_scripts" in self.state:
                    try:
                        self.state['conf_scripts'].remove(conf_script)
                        self.__log(Criticality.II, "Remove configuration script '{0}'".format(conf_script))
                        self.__save_state()
                    except:
                        pass
            else:
                self.__log(Criticality.EE, "You cannot change the set of scripts of a report that already has results. Start a new one.")
        finally:
            self.__release_lock()

    def commit_url(self):
        return self.__read_attribute__('commit_url')

    def set_commit_url(self, commit_url):
        self.__write_attribute__('commit_url', commit_url, allow_updates = True)
        self.__log(Criticality.II, "Report commit URL has been changed to '{}'".format(commit_url))

    def __task_tree_add_test__(self, task_tree, commit, test, rounds):
        if commit not in task_tree:
            task_tree[commit] = dict()
            task_tree[commit]["tests"] = dict()

        if test not in task_tree[commit]['tests']:
            task_tree[commit]['tests'][test] = dict()
            task_tree[commit]['tests'][test]['rounds'] = rounds
            total_rounds_before = 0
        else:
            total_rounds_before = task_tree[commit]['tests'][test]['rounds']
            task_tree[commit]['tests'][test]['rounds'] += rounds

        total_rounds_after = task_tree[commit]['tests'][test]['rounds']

        # if the number of rounds is equal to 0 for a test, delete it
        if task_tree[commit]['tests'][test]['rounds'] <= 0:
            del task_tree[commit]['tests'][test]
            total_rounds_after = 0

        # Delete a commit that has no test
        if len(task_tree[commit]['tests']) == 0:
            del task_tree[commit]

        return total_rounds_before, total_rounds_after

    def __add_test_unlocked__(self, commit, test, rounds, user_requested=True):
        try:
            scm = self.repo()
        except RunnerError as e:
            self.__log(Criticality.EE, "add_test: unknown repository {}".format(e))
            return 0

        if scm is not None:
            commit = scm.full_version_name(commit)

        if user_requested:
            commits = self.state['tasks']['user']['commits']
        else:
            commits = self.state['tasks']['auto']['commits']

        rounds_old, rounds_new = self.__task_tree_add_test__(commits, commit, test, rounds)

        # If we added rounds and the state was DONE, set it back to RUN
        if (rounds_new > rounds_old and
            self.__running_mode_unlocked__(check_running=False) == RunningMode.DONE):
            self.__set_running_mode_unlocked__(RunningMode.RUN)

            # If this is a user request, then remove all the "auto" tests, as
            # additional user-requested data may render these auto tests useless
            if user_requested:
                self.state['tasks']['auto']['commits'] = dict()

        return rounds_new

    def add_test(self, commit, test, rounds = None, user_requested=True):
        self.__reload_state(keep_lock=True)
        total_rounds = 0
        try:
            source = "user" if user_requested else "machine"
            self.__log(Criticality.II, "Add the {}-requested test {} ({} rounds)".format(source, test, rounds))
            total_rounds = self.__add_test_unlocked__(commit, test, rounds)
            self.__save_state()
        finally:
            self.__release_lock()
        return total_rounds

    def add_testset(self, commit, testset, rounds = 1, ensure=False, user_requested=True):
        self.__reload_state(keep_lock=True)

        try:
            self.__log(Criticality.II, "Add the testset {} ({} tests)".format(testset.name,
                                                                            len(testset)))

            for test in sorted(testset.keys()):
                if not ensure:
                    self.__add_test_unlocked__(commit, test, testset[test] * rounds,
                                               user_requested)
                else:
                    self.__force_test_rounds_unlocked__(commit, test,
                                                        testset[test] * rounds,
                                                        user_requested)

            self.__save_state()
        finally:
            self.__release_lock()

    def __force_test_rounds_unlocked__(self, commit, test, at_least, user_requested=True):
        scm = self.repo()
        if scm is not None:
            commit = scm.full_version_name(commit)

        if at_least < 1:
            return 0
        else:
            at_least = int(at_least)

        if user_requested:
            commits = self.state['tasks']['user']['commits']
        else:
            commits = self.state['tasks']['auto']['commits']

        if commit not in commits:
            commits[commit] = dict()
            commits[commit]["tests"] = dict()

        if test not in commits[commit]['tests']:
            commits[commit]['tests'][test] = dict()
            commits[commit]['tests'][test]['rounds'] = 0

        to_add = at_least - commits[commit]['tests'][test]['rounds']

        if to_add > 0:
            commits[commit]['tests'][test]['rounds'] += to_add
            return to_add
        else:
            return 0

    def force_test_rounds(self, commit, test, at_least, user_requested=True):
        ret = 0

        self.__reload_state(keep_lock=True)
        try:
            ret = self.__force_test_rounds_unlocked__(commit, test, at_least, user_requested)
            self.__save_state()
        finally:
            self.__release_lock()

        return ret

    def reset_work(self):
        self.__reload_state(keep_lock=True)
        try:
            self.__log(Criticality.II, "Reset the queued work")
            self.state['tasks']['user']['commits'] = dict()
            self.state['tasks']['auto']['commits'] = dict()
            self.__save_state()
        finally:
            self.__release_lock()

    def task_info(self):
        self._task_lock.acquire()
        tl = copy.deepcopy(self._task_list)
        c = copy.deepcopy(self._task_current)
        self._task_lock.release()

        db = TimingsDB(self.ezbench_dir + "/timing_DB")

        # Get information about the build time and the available versions
        runner = self.__create_ezbench()
        versions = set(runner.list_cached_versions())
        c_ts = db.data("build", self.profile())
        if len(c_ts) > 0:
            c_t = statistics.median(c_ts)
        else:
            c_t = None

        # the current task already has the timing information
        if c is not None:
            versions |= set([c.commit])
        if tl is not None:
            for t in tl:
                t.set_timing_information(db, c_t, versions)
                versions |= set([t.commit])

        return c, tl, self._events_str

    def __aggregate_subtests__(self, task_tree):
        for commit in task_tree:
            test_subtests = dict()
            test_rounds = dict()

            # First, read all the tests and aggregate them
            for test in task_tree[commit]["tests"]:
                basename, subtests, metric = Test.parse_name(test)
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

    def __prioritize_runs_add_by_commit__(self, task_list, task_tree, user_requested=True):
        # Add all the remaining tasks in whatever order!
        for commit in task_tree:
            for test in task_tree[commit]["tests"]:
                rounds = task_tree[commit]["tests"][test]["rounds"]
                task_list.append(TaskEntry(commit, test, rounds, user_requested=user_requested))

    def __prioritize_runs_add_deployed_first__(self, task_list, deployed_version, task_tree, user_requested=True):
        if deployed_version is not None and deployed_version in task_tree:
            for test in task_tree[deployed_version]["tests"]:
                rounds = task_tree[deployed_version]["tests"][test]["rounds"]
                task_list.append(TaskEntry(deployed_version, test, rounds,
                                           user_requested=True))
            del task_tree[deployed_version]

    def __prioritize_runs(self, task_tree_user, task_tree_auto, deployed_version, resumable_tasks):
        task_list = deque()

        # Aggregate all the subtests
        self.__aggregate_subtests__(task_tree_user)
        self.__aggregate_subtests__(task_tree_auto)

        # Schedule resumable tasks. First the already-deployed
        # versions, other versions later
        for task in resumable_tasks:
            result_file = task.get("result_file", None)
            if result_file is not None:
                entry = TaskEntry(task["version"], task["test"], 1, result_file)
            else:
                continue

            if task["version"] == deployed_version:
                task_list.appendleft(entry)
            else:
                task_list.append(entry)

            # Get rid of the task
            before, after = self.__task_tree_add_test__(task_tree_user, entry.commit, entry.test, -1)
            if before == after:
                self.__task_tree_add_test__(task_tree_auto, entry.commit, entry.test, -1)

        # Priority order: User-first, then Auto. Tests on the currently-deployed
        # version first
        self.__prioritize_runs_add_deployed_first__(task_list, deployed_version, task_tree_user, user_requested=True)
        self.__prioritize_runs_add_by_commit__(task_list, task_tree_user, user_requested=True)

        self.__prioritize_runs_add_deployed_first__(task_list, deployed_version, task_tree_auto, user_requested=False)
        self.__prioritize_runs_add_by_commit__(task_list, task_tree_auto, user_requested=False)

        return task_list

    def __change_state_to_run__(self):
        ret = False

        self.__reload_state(keep_lock=True)
        try:
            running_state=self.__running_mode_unlocked__(check_running=False)
            if running_state == RunningMode.INITIAL:
                self.__set_running_mode_unlocked__(RunningMode.RUN)
                ret = True
            elif running_state != RunningMode.RUN:
                self.__log(Criticality.II,
                        "We cannot run when the current running mode is {mode}.".format(mode=running_state.name))
                ret = False
            else:
                ret = True
        finally:
            self.__release_lock()
        return ret

    def __done_running__(self, runner):
        self._task_current = None
        self._task_list = None

        runner.done()

        # Call the hook file, telling we are done running
        self.__call_hook__('done_running_tests')

        self._task_lock.release()

    @classmethod
    def __remove_task_from_tasktree__(self, task_tree, commit, full_name, rounds):
        # Verify both that the short and long version names are used
        if commit not in task_tree:
            return 0
        if full_name not in task_tree[commit]["tests"]:
            return 0

        rounds_before =  task_tree[commit]["tests"][full_name]['rounds']
        task_tree[commit]["tests"][full_name]['rounds'] -= rounds

        if task_tree[commit]["tests"][full_name]['rounds'] <= 0:
            del task_tree[commit]["tests"][full_name]

        if len(task_tree[commit]["tests"]) == 0:
            del task_tree[commit]

        return rounds_before

    @classmethod
    def __remove_existing_tasks_from_tree(cls, report, task_tree_user, task_tree_auto):
        for commit in report.commits:
            for result in commit.results.values():
                for key in result.results():
                    full_name = Test.partial_name(result.test.full_name, [key])

                    rounds_found = len(result.result(key))
                    user_rounds = SmartEzbench.__remove_task_from_tasktree__(task_tree_user, commit.full_sha1, full_name, rounds_found)

                    SmartEzbench.__remove_task_from_tasktree__(task_tree_auto, commit.full_sha1, full_name, rounds_found - user_rounds)


    @classmethod
    def __generate_task_and_events_list__(cls, q, state, log_folder, scm):
        exit_code = 1
        task_tree_user = list()
        task_tree_auto = list()
        events_str = []
        resumable_tasks = []

        # Make sure we catch *any* error, because we need to send stuff in the
        # Queue if we do not want the parent process to get stuck
        try:
            # Generate the report, order commits based on the git history
            try:
                report = Report(log_folder, silentMode = True)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.write("\n")
                pass

            # Get the list of events
            events_str = []
            for event in report.events:
                events_str.append(str(event))

            # Walk down the report and get rid of every run that has already been made!
            task_tree_user = copy.deepcopy(state['tasks']['user']['commits'])
            task_tree_auto = copy.deepcopy(state['tasks']['auto']['commits'])
            cls.__remove_existing_tasks_from_tree(report, task_tree_user, task_tree_auto)

            resumable_tasks = report.journal.incomplete_tests()

            # Delete the tests on commits that do not compile
            for commit in report.commits:
                if commit.build_broken():
                    if commit.full_sha1 in task_tree_user:
                        del task_tree_user[commit.full_sha1]
                    elif commit.sha1 in task_tree_user:
                        del task_tree_user[commit.sha1]

                    if commit.full_sha1 in task_tree_auto:
                        del task_tree_auto[commit.full_sha1]
                    elif commit.sha1 in task_tree_auto:
                        del task_tree_auto[commit.sha1]

            exit_code = 0
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.write("\n")
            pass

        # Return the result
        q.put((exit_code, task_tree_user, task_tree_auto, events_str, resumable_tasks))

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

        self.__log(Criticality.II, "    - Configuration scripts: '{0}'".format(self.conf_scripts()))

        # Create the runner
        runner = self.__create_ezbench()
        repo_infos = runner.repo_info()
        deployed_commit = repo_infos["deployed_version"]
        self.__log(Criticality.II, "    - Deployed version: '{0}'".format(deployed_commit))
        self.__log(Criticality.II, "All the dependencies are met, generate a report...")

        # Generate a report to compare the goal with the current state. Run it
        # in a separate process because python is really bad at freeing memory
        q = multiprocessing.Queue()
        p = multiprocessing.Process(target=SmartEzbench.__generate_task_and_events_list__,
                                    args=(q, self.state, self.log_folder, self.repo()))
        p.start()
        exit_code, task_tree_user, task_tree_auto, self._events_str, resumable_tasks = q.get()
        p.join()

        if len(task_tree_user) == 0 and len(task_tree_auto) == 0:
            self.__log(Criticality.II, "Nothing left to do, exit")
            return False

        task_tree_user_str = pprint.pformat(task_tree_user)
        task_tree_auto_str = pprint.pformat(task_tree_auto)
        self.__log(Criticality.II, "Task list (user): {tsk_str}".format(tsk_str=task_tree_user_str))
        self.__log(Criticality.II, "Task list (auto): {tsk_str}".format(tsk_str=task_tree_auto_str))
        self.__log(Criticality.II, "Incomplete runs: {}".format([r['result_file'] for r in resumable_tasks]))

        # Write all the changes to the state
        self.__reload_state(keep_lock=True)
        try:
            self.__write_attribute_unlocked__('beenRunBefore', True)
            self.__save_state()
        finally:
            self.__release_lock()

        # Prioritize --> return a list of commits to do in order
        self._task_lock.acquire()
        self._task_list = self.__prioritize_runs(task_tree_user, task_tree_auto, deployed_commit, resumable_tasks)

        # Call the hook file, telling we started running
        self.__call_hook__('start_running_tests')

        # Setup the test environment
        try:
            runner.start_testing()
        except RunnerError as error:
            err_code = error.args[0]['err_code']
            msg = error.args[0]['msg']
            self.__log(Criticality.EE,
                       "Got the following error when trying to start testing: {}".format(msg))
            self.set_running_mode(RunningMode.ERROR)
            self._task_lock.release()
            return False

        # Start generating runner calls
        while len(self._task_list) > 0 and not self._deleted:
            running_mode = self.running_mode(check_running = False)
            if running_mode != RunningMode.RUN:
                self.__log(Criticality.II,
                       "Running mode changed from RUN(NING) to {mode}. Exit...".format(mode=running_mode.name))
                self.__done_running__(runner)
                return False

            self._task_current = e = self._task_list.popleft()
            short_name=e.test[:80].rsplit('|', 1)[0]+'...'
            self.__log(Criticality.DD,
                       "make {count} runs for test {test} using commit {commit}".format(count=e.rounds,
                                                                                                  commit=e.commit,
                                                                                                  test=short_name))

            # Get information about how long this task will take!
            versions = set(runner.list_cached_versions())
            db = TimingsDB(self.ezbench_dir + "/timing_DB")
            build_times = db.data("build", self.profile())
            if len(build_times) > 0:
                total_time = statistics.median(build_times)
            else:
                total_time = 0
            e.set_timing_information(db, total_time, versions)

            # Start the task
            self._task_current.started()
            for r in range(0, e.rounds):
                # Early exit if the report has been deleted
                if self._deleted:
                    break

                self.__call_hook__('start_running_test', { "task": self._task_current })

                self._task_lock.release()
                try:
                    if e.resumeResultFile is not None:
                        time, cmd_output = runner.resume(e.commit, e.test, e.resumeResultFile, False)
                    else:
                        time, cmd_output = runner.run(e.commit, e.test, False)
                    self._task_lock.acquire()
                except RunnerError as error:
                    # Acquire the lock as we are about to modify the task list
                    self._task_lock.acquire()

                    # We got an error, let's see what we can do about it!
                    err_code = error.args[0]['err_code']
                    err_str = error.args[0]['err_str']
                    if (err_code == RunnerErrorCode.CMD_TEST_EXEC_TYPE_UNSUPPORTED or
                        err_code == RunnerErrorCode.CMD_TEST_EXEC_TYPE_NEED_VALID_RESULT_FILE or
                        err_code == RunnerErrorCode.CMD_RESULT_ALREADY_COMPLETE):
                        # The result is un-resumable, schedule a full run at the end
                        self._task_list.append(TaskEntry(e.commit, e.test, 1))
                    elif err_code == RunnerErrorCode.REBOOT_NEEDED:
                        self.__done_running__(runner)
                        self.__call_hook__('reboot_needed', { "task": self._task_current })
                        self.__log(Criticality.II, "Rebooting...")
                        # We already closed the runner above, so
                        # create a temporary one for the reboot
                        Runner(self.ezbench_dir).reboot()
                        # Just in case the reboot() returned
                        os._exit(0)
                    elif (err_code.value != RunnerErrorCode.NO_ERROR and
                          err_code.value < RunnerErrorCode.COMP_DEP_UNK_ERROR.value):
                        # Error we cannot do anything about, probably a setup issue
                        # Let's mark the run as aborted until the user resets it!
                        err_msg="Ezbench runner returned the following error code: {}"
                        self.__log(Criticality.EE, err_msg.format( error.args[0]['err_code']))
                        self.set_running_mode(RunningMode.ERROR)
                    elif (err_code == RunnerErrorCode.COMPILATION_FAILED or
                          err_code == RunnerErrorCode.DEPLOYMENT_FAILED):
                        # Cancel any other test on this commit
                        self._task_list = deque([x for x in self._task_list if not x.commit == e.commit])

                        # Since we cannot compile/deploy the commit, abort all the
                        # runs scheduled for this test on this commit
                        self.__call_hook__('done_running_test', { "task": self._task_current })
                        self._task_current.round_done()
                        break

                self.__call_hook__('done_running_test', { "task": self._task_current })
                self._task_current.round_done()

        # Now that we have run everything, we can delete the "auto" tests
        self.__reload_state(keep_lock=True)
        try:
            self.state['tasks']['auto']['commits'] = dict()
            self.__save_state()
        finally:
            self.__release_lock()

        self.__done_running__(runner)
        self.__log(Criticality.II, "Done")

        return True

    def repo(self):
        if not hasattr(self, "_cache_repo_"):
            # Get the repo directory
            runner = self.__create_ezbench()
            repo_infos = runner.repo_info()
            deployed_commit = repo_infos["deployed_version"]

            self._cache_repo_ = None
            if repo_infos["path"] != '':
                if repo_infos["type"] == "git":
                    self._cache_repo_ = GitRepo(repo_infos["path"])
                    return self._cache_repo_

            # Default to no-repo
            self._cache_repo_ = NoRepo(self.log_folder)

        return self._cache_repo_

    def report(self, reorder_commits = True,
               restrict_to_commits = [], silentMode = True):
        # Generate the report, order commits based on the git history
        r = Report(self.log_folder, silentMode,
                                 restrict_to_commits = restrict_to_commits)
        r.enhance_report(self.repo())

        # Update the list of events with the most up to date report we have
        events_str = []
        for event in r.events:
            events_str.append(str(event))
        self._events_str = events_str

        return r

    # WARNING: test may be None!
    def __score_event__(self, event_commit_range, commit_sha1, test, severity):
        commit_weight = 1 - event_commit_range.average_oldness_factor()

        test_weight = 1
        if test is not None and hasattr(test, 'score_weight'):
            test_weight = test.score_weight

        return commit_weight * test_weight * severity

    def __attribute__(self, key, default):
        ret = default

        self.__reload_state(keep_lock=True)
        try:
            if "attributes" in self.state:
                ret = self.state['attributes'].get(key, default)
        finally:
            self.__release_lock()
        return ret

    @classmethod
    def attributes(cls):
        return [v.name for v in SmartEzbenchAttributes]

    def attribute(self, param):
        p = SmartEzbenchAttributes[param]
        if p == SmartEzbenchAttributes.perf_min_change:
            return self.__attribute__(param, 0.005)
        elif p == SmartEzbenchAttributes.event_min_confidence:
            return self.__attribute__(param, 0.99)
        elif p == SmartEzbenchAttributes.schedule_max_commits:
            return self.__attribute__(param, 1)
        elif p == SmartEzbenchAttributes.variance_max:
            return self.__attribute__(param, 0.025)
        elif p == SmartEzbenchAttributes.variance_max_run_count:
            return self.__attribute__(param, 20)
        elif p == SmartEzbenchAttributes.variance_min_run_count:
            return self.__attribute__(param, 2)
        elif p == SmartEzbenchAttributes.report_priority:
            return self.__attribute__(param, 0)
        elif p == SmartEzbenchAttributes.report_deadline_soft:
            return self.__attribute__(param, -1)
        elif p == SmartEzbenchAttributes.report_deadline_hard:
            return self.__attribute__(param, -1)

    def set_attribute(self, param, value):
        # verify that the attribute exists
        p = SmartEzbenchAttributes[param]

        self.__reload_state(keep_lock=True)
        try:
            if "attributes" not in self.state:
                self.state['attributes'] = dict()
            self.state['attributes'][param] = float(value)
            self.__save_state()
        finally:
            self.__release_lock()

        self.__log(Criticality.II, "Attribute '{}' set to {}".format(param, value))

    def user_data(self, key, default=None):
        ret = False
        self.__reload_state(keep_lock=True)
        try:
            ret = self.state.get('user_data', dict()).get(key, default)
        finally:
            self.__release_lock()
        return ret

    def set_user_data(self, key, value):
        self.__reload_state(keep_lock=True)
        try:
            self.state['user_data'][key] = value
            self.__save_state()
        finally:
            self.__release_lock()

    def schedule_enhancements(self):
        # Read all the attributes
        max_variance = self.attribute("variance_max")
        max_run_count = self.attribute("variance_max_run_count")
        min_run_count = self.attribute("variance_min_run_count")
        perf_diff_confidence = self.attribute("event_min_confidence")
        smallest_perf_change = self.attribute("perf_min_change")
        commit_schedule_max = self.attribute("schedule_max_commits")

        self.__log(Criticality.II, "Start enhancing the report")

        # Generate the report, order commits based on the git history
        r = Report(self.log_folder, silentMode = True)
        overlay = r.enhance_report(self.repo(), max_variance, perf_diff_confidence,
                                   smallest_perf_change)

        # Generate the list of commits to ignore when bisecting
        ignore_commits = set()
        for commit in r.commits:
            if commit.build_broken():
                ignore_commits.add(commit.full_sha1)

        # Check all events
        tasks = []
        for e in r.events:
            commit_sha1 = None
            test = None
            event_prio = 1
            severity = 0 # should be a value in [0, 1]
            test_name_to_run = ""
            runs = 0
            if type(e) is EventPerfChange or type(e) is EventRenderingChange:
                if e.commit_range.is_single_commit():
                    continue

                # ignore commits which have a big variance
                result_new = r.find_result(e.commit_range.new, e.test).result()
                if result_new.margin() > max_variance:
                    continue
                result_old = r.find_result(e.commit_range.old, e.test).result()
                if result_old.margin() > max_variance:
                    continue

                middle = e.commit_range.bisect_point(ignore_commits)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                test = e.test
                severity = min(abs(e.diff()), 1) * e.confidence
                event_prio = 0.75

                test_name_to_run = test.full_name
                runs = (len(result_old) + len(result_new)) / 2
            elif isinstance(e, EventResultNeedsMoreRuns):
                commit_sha1 = e.result.commit.full_sha1
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
                middle = e.commit_range.bisect_point(ignore_commits)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                severity = 1
                event_prio = 1
                test_name_to_run = str(e.full_name)
                runs = min_run_count
                # TODO: Remove this event and just return the merge base when
                # asking for the bisect point of the change
            elif type(e) is EventDivergingBaseResult:
                # Schedule the work
                commit_sha1 = e.merge_base
                severity = 1
                event_prio = 1
                if (e.result.test.full_name == "ezbench_runner"):
                    test_name_to_run = "no-op"
                    runs = 1
                else:
                    test_name_to_run = str(e.full_name)
                    runs = len(e.result)
            elif type(e) is EventBuildStatusChanged:
                if e.commit_range.is_single_commit():
                    continue

                # Find the middle commit
                middle = e.commit_range.bisect_point(ignore_commits)
                if middle is None:
                    continue

                # Schedule the work
                commit_sha1 = middle
                severity = 1
                event_prio = 0.1
                test_name_to_run = "no-op"
                runs = 1
            elif type(e) is EventUnitResultUnstable:
                # Nothing to do, for now
                continue
            else:
                print("schedule_enhancements: unknown event type {}".format(type(e).__name__))
                continue

            score = self.__score_event__(e.commit_range, commit_sha1, test, severity)
            score *= event_prio

            tasks.append((score, commit_sha1, test_name_to_run, runs, e))

        # Sort the tasks by priority
        tasks_sorted = sorted(tasks, key=lambda t: t[0])
        tasks_sorted_str = pprint.pformat(tasks_sorted)
        self.__log(Criticality.DD, "Needed tasks: {}".format(tasks_sorted_str))

        # If we are using the throttle mode, only schedule the commit with the
        # biggest score to speed up bisecting of the most important issues
        scheduled_commits = total_added = 0
        self.__reload_state(keep_lock=True)
        try:
            while len(tasks_sorted) > 0 and scheduled_commits < commit_schedule_max:
                commit = tasks_sorted[-1][1]
                self.__log(Criticality.DD, "Add all the tasks using commit {}".format(commit))
                for t in tasks_sorted:
                    if t[1] == commit:
                        added = self.__force_test_rounds_unlocked__(t[1], t[2], t[3], user_requested=False)
                        if added > 0:
                            self.__log(Criticality.II,
                                    "Scheduled {} more runs for the test {} on commit {}".format(added, t[2], commit))
                        total_added += added
                if total_added > 0:
                    self.__log(Criticality.II, "{}".format(t[4]))
                    scheduled_commits += 1
                else:
                    self.__log(Criticality.DD, "No work scheduled using commit {}, try another one".format(commit))
                del tasks_sorted[-1]
            if total_added > 0:
                self.__save_state()
        finally:
            self.__release_lock()

        self.__log(Criticality.II, "Done enhancing the report")

        if (total_added == 0 and
            self.__running_mode_unlocked__(check_running=False) == RunningMode.RUN):
            self.set_running_mode(RunningMode.DONE)

        return r
