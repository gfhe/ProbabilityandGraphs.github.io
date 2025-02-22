# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root
# for license information.

from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys

import debugpy
from debugpy import adapter, common, launcher
from debugpy.common import compat, fmt, json, log, messaging, sockets
from debugpy.common.compat import unicode
from debugpy.adapter import components, servers, sessions


class Client(components.Component):
    """Handles the client side of a debug session."""

    message_handler = components.Component.message_handler

    class Capabilities(components.Capabilities):
        PROPERTIES = {
            "supportsVariableType": False,
            "supportsVariablePaging": False,
            "supportsRunInTerminalRequest": False,
            "supportsMemoryReferences": False,
        }

    class Expectations(components.Capabilities):
        PROPERTIES = {
            "locale": "en-US",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": json.enum("path", optional=True),  # we don't support "uri"
        }

    def __init__(self, sock):
        if sock == "stdio":
            log.info("Connecting to client over stdio...", self)
            stream = messaging.JsonIOStream.from_stdio()
            # Make sure that nothing else tries to interfere with the stdio streams
            # that are going to be used for DAP communication from now on.
            sys.stdin = open(os.devnull, "r")
            sys.stdout = open(os.devnull, "w")
        else:
            stream = messaging.JsonIOStream.from_socket(sock)

        with sessions.Session() as session:
            super(Client, self).__init__(session, stream)

            self.client_id = None
            """ID of the connecting client. This can be 'test' while running tests."""

            self.has_started = False
            """Whether the "launch" or "attach" request was received from the client, and
            fully handled.
            """

            self.start_request = None
            """The "launch" or "attach" request as received from the client.
            """

            self._initialize_request = None
            """The "initialize" request as received from the client, to propagate to the
            server later."""

            self._deferred_events = []
            """Deferred events from the launcher and the server that must be propagated
            only if and when the "launch" or "attach" response is sent.
            """

            self._known_subprocesses = set()
            """servers.Connection instances for subprocesses that this client has been
            made aware of.
            """

            session.client = self
            session.register()

        # For the transition period, send the telemetry events with both old and new
        # name. The old one should be removed once the new one lights up.
        self.channel.send_event(
            "output",
            {
                "category": "telemetry",
                "output": "ptvsd",
                "data": {"packageVersion": debugpy.__version__},
            },
        )
        self.channel.send_event(
            "output",
            {
                "category": "telemetry",
                "output": "debugpy",
                "data": {"packageVersion": debugpy.__version__},
            },
        )

    def propagate_after_start(self, event):
        # pydevd starts sending events as soon as we connect, but the client doesn't
        # expect to see any until it receives the response to "launch" or "attach"
        # request. If client is not ready yet, save the event instead of propagating
        # it immediately.
        if self._deferred_events is not None:
            self._deferred_events.append(event)
            log.debug("Propagation deferred.")
        else:
            self.client.channel.propagate(event)

    def _propagate_deferred_events(self):
        log.debug("Propagating deferred events to {0}...", self.client)
        for event in self._deferred_events:
            log.debug("Propagating deferred {0}", event.describe())
            self.client.channel.propagate(event)
        log.info("All deferred events propagated to {0}.", self.client)
        self._deferred_events = None

    # Generic event handler. There are no specific handlers for client events, because
    # there are no events from the client in DAP - but we propagate them if we can, in
    # case some events appear in future protocol versions.
    @message_handler
    def event(self, event):
        if self.server:
            self.server.channel.propagate(event)

    # Generic request handler, used if there's no specific handler below.
    @message_handler
    def request(self, request):
        return self.server.channel.delegate(request)

    @message_handler
    def initialize_request(self, request):
        if self._initialize_request is not None:
            raise request.isnt_valid("Session is already initialized")

        self.client_id = request("clientID", "")
        self.capabilities = self.Capabilities(self, request)
        self.expectations = self.Expectations(self, request)
        self._initialize_request = request

        return {
            "supportsCompletionsRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsConfigurationDoneRequest": True,
            "supportsDebuggerProperties": True,
            "supportsDelayedStackTraceLoading": True,
            "supportsEvaluateForHovers": True,
            "supportsExceptionInfoRequest": True,
            "supportsExceptionOptions": True,
            "supportsHitConditionalBreakpoints": True,
            "supportsLogPoints": True,
            "supportsModulesRequest": True,
            "supportsSetExpression": True,
            "supportsSetVariable": True,
            "supportsValueFormattingOptions": True,
            "supportsTerminateDebuggee": True,
            "supportsGotoTargetsRequest": True,
            "exceptionBreakpointFilters": [
                {"filter": "raised", "label": "Raised Exceptions", "default": False},
                {"filter": "uncaught", "label": "Uncaught Exceptions", "default": True},
                # {"filter": "userUnhandled", "label": "User Uncaught Exceptions", "default": False},
            ],
        }

    # Common code for "launch" and "attach" request handlers.
    #
    # See https://github.com/microsoft/vscode/issues/4902#issuecomment-368583522
    # for the sequence of request and events necessary to orchestrate the start.
    def _start_message_handler(f):
        @components.Component.message_handler
        def handle(self, request):
            assert request.is_request("launch", "attach")
            if self._initialize_request is None:
                raise request.isnt_valid("Session is not initialized yet")
            if self.launcher or self.server:
                raise request.isnt_valid("Session is already started")

            self.session.no_debug = request("noDebug", json.default(False))
            if self.session.no_debug:
                servers.dont_wait_for_first_connection()

            self.session.debug_options = debug_options = set(
                request("debugOptions", json.array(unicode))
            )

            f(self, request)
            if request.response is not None:
                return

            if self.server:
                self.server.initialize(self._initialize_request)
                self._initialize_request = None

                arguments = request.arguments
                if self.launcher:
                    if "RedirectOutput" in debug_options:
                        # The launcher is doing output redirection, so we don't need the
                        # server to do it, as well.
                        arguments = dict(arguments)
                        arguments["debugOptions"] = list(
                            debug_options - {"RedirectOutput"}
                        )

                    if arguments.get("redirectOutput"):
                        arguments = dict(arguments)
                        del arguments["redirectOutput"]

                # pydevd doesn't send "initialized", and responds to the start request
                # immediately, without waiting for "configurationDone". If it changes
                # to conform to the DAP spec, we'll need to defer waiting for response.
                try:
                    self.server.channel.request(request.command, arguments)
                except messaging.NoMoreMessages:
                    # Server closed connection before we could receive the response to
                    # "attach" or "launch" - this can happen when debuggee exits shortly
                    # after starting. It's not an error, but we can't do anything useful
                    # here at this point, either, so just bail out.
                    request.respond({})
                    self.session.finalize(
                        fmt(
                            "{0} disconnected before responding to {1!j}",
                            self.server,
                            request.command,
                        )
                    )
                    return
                except messaging.MessageHandlingError as exc:
                    exc.propagate(request)

            if self.session.no_debug:
                self.start_request = request
                self.has_started = True
                request.respond({})
                self._propagate_deferred_events()
                return

            if {"WindowsClient", "Windows"} & debug_options:
                client_os_type = "WINDOWS"
            elif {"UnixClient", "UNIX"} & debug_options:
                client_os_type = "UNIX"
            else:
                client_os_type = "WINDOWS" if sys.platform == "win32" else "UNIX"
            self.server.channel.request(
                "setDebuggerProperty",
                {
                    "skipSuspendOnBreakpointException": ("BaseException",),
                    "skipPrintBreakpointException": ("NameError",),
                    "multiThreadsSingleNotification": True,
                    "ideOS": client_os_type,
                },
            )

            # Let the client know that it can begin configuring the adapter.
            self.channel.send_event("initialized")

            self.start_request = request
            return messaging.NO_RESPONSE  # will respond on "configurationDone"

        return handle

    @_start_message_handler
    def launch_request(self, request):
        from debugpy.adapter import launchers

        if self.session.id != 1 or len(servers.connections()):
            raise request.cant_handle('"attach" expected')

        debug_options = set(request("debugOptions", json.array(unicode)))

        # Handling of properties that can also be specified as legacy "debugOptions" flags.
        # If property is explicitly set to false, but the flag is in "debugOptions", treat
        # it as an error. Returns None if the property wasn't explicitly set either way.
        def property_or_debug_option(prop_name, flag_name):
            assert prop_name[0].islower() and flag_name[0].isupper()

            value = request(prop_name, bool, optional=True)
            if value == ():
                value = None

            if flag_name in debug_options:
                if value is False:
                    raise request.isnt_valid(
                        '{0!j}:false and "debugOptions":[{1!j}] are mutually exclusive',
                        prop_name,
                        flag_name,
                    )
                value = True

            return value

        # "pythonPath" is a deprecated legacy spelling. If "python" is missing, then try
        # the alternative. But if both are missing, the error message should say "python".
        python_key = "python"
        if python_key in request:
            if "pythonPath" in request:
                raise request.isnt_valid(
                    '"pythonPath" is not valid if "python" is specified'
                )
        elif "pythonPath" in request:
            python_key = "pythonPath"
        python = request(python_key, json.array(unicode, vectorize=True, size=(0,)))
        if not len(python):
            python = [compat.filename(sys.executable)]
            
        python += request("pythonArgs", json.array(unicode, size=(0,)))
        request.arguments["pythonArgs"] = python[1:]

        program = module = code = ()
        if "program" in request:
            program = request("program", unicode)
            args = [program]
            request.arguments["processName"] = program
        if "module" in request:
            module = request("module", unicode)
            args = ["-m", module]
            request.arguments["processName"] = module
        if "code" in request:
            code = request("code", json.array(unicode, vectorize=True, size=(1,)))
            args = ["-c", "\n".join(code)]
            request.arguments["processName"] = "-c"

        num_targets = len([x for x in (program, module, code) if x != ()])
        if num_targets == 0:
            raise request.isnt_valid(
                'either "program", "module", or "code" must be specified'
            )
        elif num_targets != 1:
            raise request.isnt_valid(
                '"program", "module", and "code" are mutually exclusive'
            )

        # Propagate "args" via CLI if and only if shell expansion is requested.
        args_expansion = request(
            "argsExpansion", json.enum("shell", "none", optional=True)
        )
        if args_expansion == "shell":
            args += request("args", json.array(unicode))
            request.arguments.pop("args", None)

        cwd = request("cwd", unicode, optional=True)
        if cwd == ():
            # If it's not specified, but we're launching a file rather than a module,
            # and the specified path has a directory in it, use that.
            cwd = None if program == () else (os.path.dirname(program) or None)

        console = request(
            "console",
            json.enum(
                "internalConsole",
                "integratedTerminal",
                "externalTerminal",
                optional=True,
            ),
        )
        console_title = request("consoleTitle", json.default("Python Debug Console"))

        sudo = bool(property_or_debug_option("sudo", "Sudo"))
        if sudo and sys.platform == "win32":
            raise request.cant_handle('"sudo":true is not supported on Windows.')

        launcher_path = request("debugLauncherPath", os.path.dirname(launcher.__file__))
        adapter_host = request("debugAdapterHost", "127.0.0.1")

        servers.serve(adapter_host)
        launchers.spawn_debuggee(
            self.session,
            request,
            python,
            launcher_path,
            adapter_host,
            args,
            cwd,
            console,
            console_title,
            sudo,
        )

    @_start_message_handler
    def attach_request(self, request):
        if self.session.no_debug:
            raise request.isnt_valid('"noDebug" is not supported for "attach"')

        host = request("host", unicode, optional=True)
        port = request("port", int, optional=True)
        listen = request("listen", dict, optional=True)
        connect = request("connect", dict, optional=True)
        pid = request("processId", (int, unicode), optional=True)
        sub_pid = request("subProcessId", int, optional=True)

        if host != () or port != ():
            if listen != ():
                raise request.isnt_valid(
                    '"listen" and "host"/"port" are mutually exclusive'
                )
            if connect != ():
                raise request.isnt_valid(
                    '"connect" and "host"/"port" are mutually exclusive'
                )
        if listen != ():
            if connect != ():
                raise request.isnt_valid(
                    '"listen" and "connect" are mutually exclusive'
                )
            if pid != ():
                raise request.isnt_valid(
                    '"listen" and "processId" are mutually exclusive'
                )
            if sub_pid != ():
                raise request.isnt_valid(
                    '"listen" and "subProcessId" are mutually exclusive'
                )
        if pid != () and sub_pid != ():
            raise request.isnt_valid(
                '"processId" and "subProcessId" are mutually exclusive'
            )

        if listen != ():
            host = listen("host", "127.0.0.1")
            port = listen("port", int)
            adapter.access_token = None
            host, port = servers.serve(host, port)
        else:
            host, port = servers.serve()

        # There are four distinct possibilities here.
        #
        # If "processId" is specified, this is attach-by-PID. We need to inject the
        # debug server into the designated process, and then wait until it connects
        # back to us. Since the injected server can crash, there must be a timeout.
        #
        # If "subProcessId" is specified, this is attach to a known subprocess, likely
        # in response to a "debugpyAttach" event. If so, the debug server should be
        # connected already, and thus the wait timeout is zero.
        #
        # If "listen" is specified, this is attach-by-socket with the server expected
        # to connect to the adapter via debugpy.connect(). There is no PID known in
        # advance, so just wait until the first server connection indefinitely, with
        # no timeout.
        #
        # If "connect" is specified, this is attach-by-socket in which the server has
        # spawned the adapter via debugpy.listen(). There is no PID known to the client
        # in advance, but the server connection should be either be there already, or
        # the server should be connecting shortly, so there must be a timeout.
        #
        # In the last two cases, if there's more than one server connection already,
        # this is a multiprocess re-attach. The client doesn't know the PID, so we just
        # connect it to the oldest server connection that we have - in most cases, it
        # will be the one for the root debuggee process, but if it has exited already,
        # it will be some subprocess.
        if pid != ():
            if not isinstance(pid, int):
                try:
                    pid = int(pid)
                except Exception:
                    raise request.isnt_valid('"processId" must be parseable as int')
            debugpy_args = request("debugpyArgs", json.array(unicode))
            servers.inject(pid, debugpy_args)
            timeout = common.PROCESS_SPAWN_TIMEOUT
            pred = lambda conn: conn.pid == pid
        else:
            if sub_pid == ():
                pred = lambda conn: True
                timeout = common.PROCESS_SPAWN_TIMEOUT if listen == () else None
            else:
                pred = lambda conn: conn.pid == sub_pid
                timeout = 0

        self.channel.send_event("debugpyWaitingForServer", {"host": host, "port": port})
        conn = servers.wait_for_connection(self.session, pred, timeout)
        if conn is None:
            if sub_pid != ():
                # If we can't find a matching subprocess, it's not always an error -
                # it might have already exited, or didn't even get a chance to connect.
                # To prevent the client from complaining, pretend that the "attach"
                # request was successful, but that the session terminated immediately.
                request.respond({})
                self.session.finalize(
                    fmt('No known subprocess with "subProcessId":{0}', sub_pid)
                )
                return

            raise request.cant_handle(
                (
                    "Timed out waiting for debug server to connect."
                    if timeout
                    else "There is no debug server connected to this adapter."
                ),
                sub_pid,
            )

        try:
            conn.attach_to_session(self.session)
        except ValueError:
            request.cant_handle("{0} is already being debugged.", conn)

    @message_handler
    def configurationDone_request(self, request):
        if self.start_request is None or self.has_started:
            request.cant_handle(
                '"configurationDone" is only allowed during handling of a "launch" '
                'or an "attach" request'
            )

        try:
            self.has_started = True
            try:
                result = self.server.channel.delegate(request)
            except messaging.NoMoreMessages:
                # Server closed connection before we could receive the response to
                # "configurationDone" - this can happen when debuggee exits shortly
                # after starting. It's not an error, but we can't do anything useful
                # here at this point, either, so just bail out.
                request.respond({})
                self.start_request.respond({})
                self.session.finalize(
                    fmt(
                        "{0} disconnected before responding to {1!j}",
                        self.server,
                        request.command,
                    )
                )
                return
            else:
                request.respond(result)
        except messaging.MessageHandlingError as exc:
            self.start_request.cant_handle(str(exc))
        finally:
            if self.start_request.response is None:
                self.start_request.respond({})
                self._propagate_deferred_events()

        # Notify the client of any child processes of the debuggee that aren't already
        # being debugged.
        for conn in servers.connections():
            if conn.server is None and conn.ppid == self.session.pid:
                self.notify_of_subprocess(conn)

    @message_handler
    def evaluate_request(self, request):
        propagated_request = self.server.channel.propagate(request)

        def handle_response(response):
            request.respond(response.body)

        propagated_request.on_response(handle_response)

        return messaging.NO_RESPONSE

    @message_handler
    def pause_request(self, request):
        request.arguments["threadId"] = "*"
        return self.server.channel.delegate(request)

    @message_handler
    def continue_request(self, request):
        request.arguments["threadId"] = "*"

        try:
            return self.server.channel.delegate(request)
        except messaging.NoMoreMessages:
            # pydevd can sometimes allow the debuggee to exit before the queued
            # "continue" response gets sent. Thus, a failed "continue" response
            # indicating that the server disconnected should be treated as success.
            return {"allThreadsContinued": True}

    @message_handler
    def debugpySystemInfo_request(self, request):
        result = {"debugpy": {"version": debugpy.__version__}}
        if self.server:
            try:
                pydevd_info = self.server.channel.request("pydevdSystemInfo")
            except Exception:
                # If the server has already disconnected, or couldn't handle it,
                # report what we've got.
                pass
            else:
                result.update(pydevd_info)
        return result

    @message_handler
    def terminate_request(self, request):
        self.session.finalize('client requested "terminate"', terminate_debuggee=True)
        return {}

    @message_handler
    def disconnect_request(self, request):
        terminate_debuggee = request("terminateDebuggee", bool, optional=True)
        if terminate_debuggee == ():
            terminate_debuggee = None
        self.session.finalize('client requested "disconnect"', terminate_debuggee)
        return {}

    def notify_of_subprocess(self, conn):
        with self.session:
            if self.start_request is None or conn in self._known_subprocesses:
                return
            if "processId" in self.start_request.arguments:
                log.warning(
                    "Not reporting subprocess for {0}, because the parent process "
                    'was attached to using "processId" rather than "port".',
                    self.session,
                )
                return

            log.info("Notifying {0} about {1}.", self, conn)
            body = dict(self.start_request.arguments)
            self._known_subprocesses.add(conn)

        for key in "processId", "listen", "preLaunchTask", "postDebugTask":
            body.pop(key, None)

        body["name"] = fmt("Subprocess {0}", conn.pid)
        body["request"] = "attach"
        body["subProcessId"] = conn.pid

        for key in "args", "processName", "pythonArgs":
            body.pop(key, None)

        host = body.pop("host", None)
        port = body.pop("port", None)
        if "connect" not in body:
            body["connect"] = {}
        if "host" not in body["connect"]:
            body["connect"]["host"] = host if host is not None else "127.0.0.1"
        if "port" not in body["connect"]:
            if port is None:
                _, port = listener.getsockname()
            body["connect"]["port"] = port

        self.channel.send_event("debugpyAttach", body)


def serve(host, port):
    global listener
    listener = sockets.serve("Client", Client, host, port)
    return listener.getsockname()


def stop_serving():
    try:
        listener.close()
    except Exception:
        log.swallow_exception(level="warning")
