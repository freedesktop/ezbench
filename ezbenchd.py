#!/usr/bin/env python3

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

import multiprocessing
import argparse
import signal
import time
import sys
import os

from datetime import datetime

ezbench_dir = os.path.dirname(os.path.abspath(__file__))

sys.path.append(os.path.join(ezbench_dir, 'python-modules'))

from ezbench.smartezbench import *

from stats import compare_reports

def setup_http_server(bind_ip = "0.0.0.0", port = 8080):
    from mako.template import Template
    import http.server
    import socket
    import threading
    import socketserver

    list_template = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">

<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>Ezbenchd: Status page</title>
</head>

<body>
    <h1>Ezbenchd: Status page</h1>
    <h2>Reports</h2>
    <p>Here is the list of available reports</p>
    <ul>
        % for sbench in sbenches:
        <%
            sbench = sbenches[sbench]
            report_name = sbench.report_name
        %>
        <li>${report_name}: <a href="/file/${report_name}/">report</a>, <a href="/status/${report_name}/">status</a> (${sbench.running_mode().name})</li>
        % endfor
    </ul>
</body>
</html>
"""

    status_template = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">

<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>Ezbenchd: Status page</title>
<style>
    a.button {
        -webkit-appearance: button;
        -moz-appearance: button;
        appearance: button;

        text-decoration: none;
        color: initial;
        padding: 3px;
    }
</style>
</head>

<%
    from datetime import timedelta

    mode = sbench.running_mode().name

    task_cur, task_list, events = sbench.task_info()

    total_time_left = 0
    if task_cur is not None:
        time = task_cur.remaining_time().total_seconds()
        if time > 0:
            total_time_left += time

    if task_list is not None:
        for task in task_list:
            if task is None:
                continue
            time = task.remaining_time().total_seconds()
            if time > 0:
                total_time_left += time
    total_time_left = timedelta(seconds=int(total_time_left))
%>

<body>
    <h1>Ezbenchd report '${report_name}'</h1>
    <h2>Status</h2>
    <p>General information about the report</p>
    <table>
        <tr><th>Name</th><th>Value</th><th>Actions</th></tr>
        <tr><td>Report name</td><td>${report_name}</td><td></td></tr>
        <tr><td>Running mode</td><td>${mode}</td><td>
            % if mode != "RUN" and mode != "RUNNING":
            <a href="/mode/${report_name}/run" class="button">Run</a>
            % else:
            <a href="/mode/${report_name}/pause" class="button">Pause</a>
            % endif
        </td></tr>
        <tr><td>Log file</td><td></td><td><a href="/file/${report_name}/smartezbench.log" class="button">View</a></td></tr>
    </table>
    <h2>Tasks</h2>
    % if task_cur is not None:
    <p>Current task: ${task_cur}</p>
    %endif

    <p><ul>
    % if task_list is not None and len(task_list) > 0:
    % for task in task_list:
        <li>${task}</li>
    % endfor
    %elif task_list is not None:
    <li>No tasks left</li>
    % else:
    <li>Unknown task list</li>
    % endif
    </ul></p>
    <p>Total remaining time: ${total_time_left}s</p>

    <h2>Events</h2>
    <ul>
        % if events is not None and len(events) > 0:
            % for event in events:
        <li>${event}</li>
            % endfor
        % else:
            <li>No events</li>
        % endif
    </ul>
</body>
</html>
"""

    class CustomHTTPHandler(http.server.SimpleHTTPRequestHandler):
        def parse_request(self, *args, **kwargs):
            return super().parse_request(*args, **kwargs)

        def __serve_file__(self, report_name, filename, content_type = "text/plain"):
            msg = "unknown error"

            if not filename:
                filename = "index.html"

            content_types = {"html": "text/html", "png": "image/png"}

            extension = os.path.splitext(filename)[1][1:]
            if extension in content_types:
                content_type = content_types[extension]

            chroot_folder = "{}/logs/{}".format(ezbench_dir, report_name)
            path = "{}/{}".format(chroot_folder, filename)
            real_path = os.path.realpath(path)
            if real_path.startswith(chroot_folder):
                try:
                    with open(real_path, 'rb') as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        f.seek(0, os.SEEK_SET)

                        self.send_response(200)
                        self.send_header("Content-type", content_type)
                        self.send_header("Content-length", size)
                        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                        self.send_header("Pragma", "no-cache")
                        self.send_header("Expires", "0")
                        self.end_headers()

                        while True:
                            data = f.read(1024)
                            if not data:
                                break
                            self.wfile.write(data)
                        return
                except Exception as e:
                    print("WARNING: An exception got raised while reading file '{}': {}".format(real_path, e))
                    msg = "Invalid file name"
                    pass
            else:
                print("WARNING: Tried to serve a file ('{}') outside of our chroot ('{}')".format(real_path, chroot_folder))
                msg = "Invalid path"

            array = str.encode(msg)
            self.send_response(404)
            self.send_header("Content-type", content_type)
            self.send_header("Content-length", len(array))
            self.end_headers()
            self.wfile.write(array)

        def do_GET(self):
            response = 200
            loc = ""
            html = ""
            m = re.search("^/([a-z]+)/(.*)/(.*)$", self.path)
            if m is not None and len(m.groups()) >= 2:
                cmd = m.groups()[0]
                report_name = m.groups()[1]
                args = m.groups()[2]

                if cmd != "" and report_name != "" and report_name in sbenches:
                    if cmd == "file":
                        return self.__serve_file__(report_name, args)
                    elif cmd == "mode" or cmd == "status":
                        sbench = sbenches[report_name]
                        if cmd == "mode":
                            if args == "run":
                                sbench.set_running_mode(RunningMode.RUN)
                                loc = "/status/{}/".format(report_name)
                            elif args == "pause":
                                sbench.set_running_mode(RunningMode.PAUSE)
                                loc = "/status/{}/".format(report_name)
                            else:
                                html = "Invalid mode '{}'".format(args)

                        html = Template(status_template).render(sbench=sbench,
                                                                report_name=report_name)
                else:
                    response = 404
                    html = "Report name '{}' does not exist".format(report_name)

            if html == "" and loc == "":
                html = Template(list_template).render(sbenches=sbenches)

            if loc != "":
                self.send_response(302)
                self.send_header('Location', loc)
            else:
                # Add a footer
                if response == 200:
                    date = datetime.now().strftime("%A, %d. %B %Y %H:%M:%S")
                    f = "<footer>Autogenerated by Ezbenchd on {}.</footer>".format(date)
                    html += f

                self.send_response(response)
                self.send_header("Content-type", "text/html")
                self.send_header("Content-length", len(html))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(str.encode(html))

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        pass


    server = ThreadedTCPServer((bind_ip, port), CustomHTTPHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    setup_http_server.server = server
    setup_http_server.server_thread = server_thread

def teardown_htttp_server():
    setup_http_server.server.shutdown()
    setup_http_server.server.server_close()

stop_requested = False

def stop_handler(signum, frame):
    global stop_requested
    stop_requested = True
    print("-- The user requested to abort! --")
    # TODO: Abort faster than after every run
    return

def reload_conf_handler(signum, frame):
    # TODO
    return


# parse the options
parser = argparse.ArgumentParser()
parser.add_argument("--embed", action="store_true")
parser.add_argument("--http_server", help="Generate an HTTP interface to show the status of the reports. Format: listen_ip:port")
args = parser.parse_args()

# Set up the http server
if args.http_server is not None:
    fields = args.http_server.split(":")
    setup_http_server(fields[0], int(fields[1]))

# handle the signals systemd asks us to
signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGHUP, reload_conf_handler)

reportStateModDate = dict()
sbenches = dict()

def sbench_run(report_name):
    sbench = SmartEzbench(ezbench_dir, report_name)
    report = sbench.schedule_enhancements()

    # Generate an HTML with the report returned by schedule_enhancements
    clock_start = time.clock()
    compare_reports.reports_to_html([report],
                                    "{}/logs/{}/index.html".format(ezbench_dir, sbench.report_name),
                                    output_unit = "fps",
                                    commit_url = sbench.commit_url(),
                                    verbose = False,
                                    embed = args.embed)
    print("Generated an HTML report in {:.2f} seconds".format(time.clock() - clock_start))

lastPoll = 0
while not stop_requested:
    futureLastPoll = time.time()
    reports = list_smart_ezbench_report_names(ezbench_dir, lastPoll)
    lastPoll = futureLastPoll
    for report_name in reports:
        try:
            if report_name not in sbenches:
                sbench = SmartEzbench(ezbench_dir, report_name)
                sbenches[report_name] = sbench
            else:
                sbench = sbenches[report_name]
            if sbench.running_mode() == RunningMode.RUN:
                sbench.run()

                # Run the report generation in a separate process because python
                # is really bad at freeing memory
                p = multiprocessing.Process(target=sbench_run,
                                    args=(report_name,))
                p.start()
                p.join()

        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.write("\n")
            pass

    # TODO: Replace this by inotify
    time.sleep(1)

# Tear down the http server
if args.http_server is not None:
    teardown_htttp_server()
