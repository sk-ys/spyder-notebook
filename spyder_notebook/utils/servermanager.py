# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License

"""File implementing ServerManager."""

# Standard library imports
import datetime
import enum
import json
import logging
import os.path as osp
import sys

# Qt imports
from qtpy.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

# Third-party imports
from jupyter_core.paths import jupyter_runtime_dir
from notebook import notebookapp

# Spyder imports
from spyder.config.base import DEV, get_home_dir, get_module_path


# Kernel specification to use in notebook server
KERNELSPEC = 'spyder.plugins.ipythonconsole.utils.kernelspec.SpyderKernelSpec'

# Delay we wait to check whether server is up (in ms)
CHECK_SERVER_UP_DELAY = 250

# Delay before we give up on server starting (in s)
SERVER_TIMEOUT_DELAY = 30

logger = logging.getLogger(__name__)


class ServerState(enum.Enum):
    """State of a server process."""

    STARTING = 1
    RUNNING = 2
    FINISHED = 3
    ERROR = 4
    TIMED_OUT = 5


class ServerProcess():
    """
    Process executing a notebook server.

    This is a data class.
    """

    def __init__(self, process, notebook_dir, starttime=None,
                 state=ServerState.STARTING, server_info=None):
        """
        Construct a ServerProcess.

        Parameters
        ----------
        process : QProcess
            The process described by this instance.
        notebook_dir : str
            Directory from which the server can render notebooks.
        starttime : datetime or None, optional
            Time at which the process was started. The default is None,
            meaning that the current time should be used.
        state : ServerState, optional
            State of the server process. The default is ServerState.STARTING.
        server_info : dict or None, optional
            If set, this is a dict with information given by the server in
            a JSON file in jupyter_runtime_dir(). It has keys like 'url' and
            'token'. The default is None.
        """
        self.process = process
        self.notebook_dir = notebook_dir
        self.starttime = starttime or datetime.datetime.now()
        self.state = state
        self.server_info = server_info


class ServerManager(QObject):
    """
    Manager for notebook servers.

    A Jupyter notebook server will only render notebooks under a certain
    directory, so we may need several servers. This class manages all these
    servers.

    Attributes
    ----------
    dark_theme : bool
        Whether notebooks should be rendered using the dark theme.
    servers : list of ServerProcess
        List of servers managed by this object.
    """

    # A server has started and is now accepting requests
    sig_server_started = Signal()

    # We tried to start a server but it took too long to start up
    sig_server_timed_out = Signal()

    def __init__(self, dark_theme=False):
        """
        Construct a ServerManager.

        Parameters
        ----------
        dark_theme : bool, optional
            Whether notebooks should be rendered using the dark theme.
            The default is False.
        """
        super().__init__()
        self.dark_theme = dark_theme
        self.servers = []

    def get_server(self, filename, start=True):
        """
        Return server which can render a notebook or potentially start one.

        Return the server info of a server managed by this object which can
        render the notebook with the given file name. If no such server
        exists and `start` is True, then start up a server asynchronously
        (unless a suitable server is already in the process of starting up).

        Parameters
        ----------
        filename : str
            File name of notebook which is to be rendered.
        start : bool, optional
            Whether to start up a server if none exists. The default is True.

        Returns
        -------
        dict or None
            A dictionary describing the server which can render the notebook,
            or None if no such server exists.
        """
        filename = osp.abspath(filename)
        for server in self.servers:
            if filename.startswith(server.notebook_dir):
                if server.state == ServerState.RUNNING:
                    return server.server_info
                elif server.state == ServerState.STARTING:
                    logger.debug('Waiting for server for %s to start up',
                                 server.notebook_dir)
                    return None
        if start:
            self.start_server(filename)
        return None

    def start_server(self, filename):
        """
        Start a notebook server asynchronously.

        Start a server which can render the given notebook and return
        immediately. The manager will check periodically whether the server is
        accepting requests and emit `sig_server_started` or
        `sig_server_timed_out` when appropriate.

        Parameters
        ----------
        filename : str
            File name of notebook to be rendered by the server.
        """
        home_dir = get_home_dir()
        if filename.startswith(home_dir):
            nbdir = home_dir
        else:
            nbdir = osp.dirname(filename)

        logger.debug('Starting new notebook server for %s', nbdir)
        process = QProcess(None)
        process.setProcessChannelMode(QProcess.ForwardedChannels)
        serverscript = osp.join(osp.dirname(__file__), '../server/main.py')
        serverscript = osp.normpath(serverscript)
        arguments = [serverscript, '--no-browser',
                     '--notebook-dir={}'.format(nbdir),
                     '--NotebookApp.password=',
                     "--KernelSpecManager.kernel_spec_class='{}'".format(
                           KERNELSPEC)]
        if self.dark_theme:
            arguments.append('--dark')

        if DEV:
            env = QProcessEnvironment.systemEnvironment()
            env.insert('PYTHONPATH', osp.dirname(get_module_path('spyder')))
            process.setProcessEnvironment(env)

        process.start(sys.executable, arguments)
        server_process = ServerProcess(process, notebook_dir=nbdir)
        self.servers.append(server_process)

        self._check_server_started(server_process)

    def _check_server_started(self, server_process):
        """
        Check whether a notebook server has started up.

        Look for a json file in the Jupyter runtime dir to check whether the
        notebook server has started up. If so, then emit `sig_server_started`
        and fill the server info with the contents of the json file. If not,
        then schedule another check after a short delay (CHECK_SERVER_UP_DELAY)
        unless the server is taken too long (SERVER_TIMEOUT_DELAY). In the
        latter case, emit `sig_server_timed_out`.

        Parameters
        ----------
        server_process : ServerProcess
            The server process to be checked.
        """
        pid = server_process.process.processId()
        runtime_dir = jupyter_runtime_dir()
        filename = osp.join(runtime_dir, 'nbserver-{}.json'.format(pid))

        try:
            with open(filename, encoding='utf-8') as f:
                server_info = json.load(f)
        except OSError:  # E.g., file does not exist
            delay = datetime.datetime.now() - server_process.starttime
            if delay > datetime.timedelta(seconds=SERVER_TIMEOUT_DELAY):
                logger.debug('Notebook server for %s timed out',
                             server_process.notebook_dir)
                server_process.state = ServerState.TIMED_OUT
                self.sig_server_timed_out.emit()
            else:
                QTimer.singleShot(
                    CHECK_SERVER_UP_DELAY,
                    lambda: self._check_server_started(server_process))
            return None

        logger.debug('server started')
        server_process.state = ServerState.RUNNING
        server_process.server_info = server_info
        self.sig_server_started.emit()

    def shutdown_all_servers(self):
        """Shutdown all running servers."""
        for server in self.servers:
            if server.state == ServerState.RUNNING:
                logger.debug('Shutting down notebook server for %s',
                             server.notebook_dir)
                notebookapp.shutdown_server(server.server_info)
                server.state = ServerState.FINISHED
