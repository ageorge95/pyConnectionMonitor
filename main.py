import sys
import os
import ctypes
import threading
import socket
import json
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from traceback import format_exc
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from pickle import dump, load
from time import sleep
from logging import getLogger
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QSizePolicy, QMessageBox,
    QLineEdit, QInputDialog
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Signal, Slot
from ag95 import configure_logger

# Configure logging
configure_logger(log_level='INFO')
logger = getLogger(__name__)

SETTINGS_FILE = "settings.json"

APP_ID = 'AG95.pyConnectionMonitor'
ICON_FILE = 'icon.ico'

DEFAULT_SERVER = '8.8.8.8:53'
MAX_RECENT_SERVERS = 10

def parse_server(address: str):
    """Validate a 'host:port' string. Returns (host, port) or raises ValueError."""
    address = address.strip()
    if ':' not in address:
        raise ValueError("Expected the format host:port, for example 8.8.8.8:53")
    host, port_text = address.rsplit(':', 1)
    host = host.strip()
    if not host:
        raise ValueError("The host must not be empty.")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"The port '{port_text}' is not a number.")
    if not 1 <= port <= 65535:
        raise ValueError(f"The port {port} must be between 1 and 65535.")
    return host, port

def get_running_path(relative_path):
    """Return a bundled file's path in both source and PyInstaller builds."""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def set_windows_app_id():
    """Give the app its own Windows taskbar identity."""
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass

class IOHandlerCheckData:
    def __init__(self, server: str):
        self.server = server.replace(':', '_')
        self.log = getLogger(self.__class__.__name__)

    def save(self, data):
        try:
            with open(f"{self.server}.pkl", 'wb') as f:
                dump(data, f)
            self.log.info(f"Saved state to {self.server}.pkl")
        except Exception as e:
            self.log.error(f"Failed saving: {e}")

    def load(self):
        if os.path.exists(f"{self.server}.pkl"):
            try:
                with open(f"{self.server}.pkl", 'rb') as f:
                    return load(f)
            except Exception as e:
                self.log.error(f"Failed loading: {e}")
        return []

class IOHandlerSettingsData:

    @staticmethod
    def save_settings(settings_data):
        """Saves the provided dictionary to the settings JSON file."""
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings_data, f, indent=4)
            logger.info(f"Saved settings to {SETTINGS_FILE}")
        except Exception as e:
            logger.error(f"Failed saving settings: {e}")

    @staticmethod
    def load_settings():
        """Loads settings from the JSON file, returning defaults if not found."""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed loading settings: {e}")
        return {}  # Return empty dict if no file or error

class InternetChecker:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = getLogger(self.__class__.__name__)

    def is_online(self) -> bool:
        # First check DNS resolution
        try:
            addrinfo = socket.getaddrinfo(self.host, self.port,
                                          family=socket.AF_INET,
                                          type=socket.SOCK_STREAM)
        except Exception as e:
            self.log.warning(f"DNS failed for {self.host}: {str(e)}")
            return False

        # Then check connection
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
                return True
        except Exception as e:
            self.log.warning(f"Connection failed to {self.host}:{self.port}: {str(e)}")
            return False

def merge_segments(check_data):
    """Merge consecutive checks with the same status into color spans and prune old entries."""
    # decimate data to show color spans instead of lines
    MAX_GAP = timedelta(seconds=35)

    merged = []
    for seg in check_data:
        if merged:
            last = merged[-1]
            gap = seg['start'] - last['end']
            if seg['status'] == last['status'] and gap <= MAX_GAP:
                # contiguous & same colour → extend
                last['end'] = seg['end']
                continue
        # first entry, gap too large or colour change → new segment
        merged.append({'start': seg['start'],
                       'end': seg['end'],
                       'status': seg['status']})

    # Data pruning logic
    max_history = 7 * 86400  # Base retention
    buffer_window = 300  # Keep extra 5 minutes for merging context
    cutoff = datetime.now() - timedelta(seconds=max_history + buffer_window)

    # Only prune entries that are fully outside buffer window
    return [entry for entry in merged
            if entry['end'] > cutoff or
            (entry['end'] - entry['start']) > timedelta(seconds=300)]

class MonitoredServer:
    """One monitored host:port together with its check history."""

    def __init__(self, address: str):
        host, port = parse_server(address)
        self.address = f"{host}:{port}"
        self.checker = InternetChecker(host, port)
        self.io_check_data = IOHandlerCheckData(self.address)
        self.data = list(self.io_check_data.load())
        self.online = None  # None = not checked yet

    def save(self):
        self.io_check_data.save(self.data)

class ServerRow(QWidget):
    """Label shown to the left of a server's plot: alias, address, status and edit/remove buttons."""

    def __init__(self, address: str, alias: str, on_remove, on_rename, parent=None):
        super().__init__(parent)
        self.address = address
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)

        text = QVBoxLayout()
        text.addStretch()
        self.name_label = QLabel()
        self.name_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.name_label.setWordWrap(True)  # long aliases wrap instead of widening the column
        text.addWidget(self.name_label)
        self.address_label = QLabel(address)
        self.address_label.setStyleSheet("color: gray;")
        text.addWidget(self.address_label)
        self.status_label = QLabel()
        text.addWidget(self.status_label)
        text.addStretch()
        layout.addLayout(text, 1)

        rename_btn = QPushButton("✎")
        rename_btn.setFixedWidth(28)
        rename_btn.setToolTip(f"Change the alias of {address}")
        rename_btn.clicked.connect(lambda: on_rename(address))
        layout.addWidget(rename_btn)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(28)
        remove_btn.setToolTip(f"Stop monitoring {address}")
        remove_btn.clicked.connect(lambda: on_remove(address))
        layout.addWidget(remove_btn)

        self.set_alias(alias)
        self.set_status(None)

    def set_alias(self, alias: str):
        """Shows the alias in bold with the address below it; without an alias only the address is shown."""
        self.name_label.setText(alias or self.address)
        self.address_label.setVisible(bool(alias))
        self.setToolTip(f"{alias} ({self.address})" if alias else self.address)

    def set_status(self, online):
        if online is None:
            self.status_label.setText("CHECKING...")
            self.status_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        else:
            self.status_label.setText('ONLINE' if online else 'OFFLINE')
            self.status_label.setStyleSheet(
                f"font-size: 14px; font-weight: bold; color: {'green' if online else 'red'};")

class MainWindow(QMainWindow):
    update_plot_signal = Signal()

    def __init__(self):
        super().__init__()
        self.io_settings_data = IOHandlerSettingsData
        self.settings_data = self.io_settings_data.load_settings()

        self.check_data_lock = threading.Lock()
        self.settings_data_lock = threading.Lock()

        self._stop_event = threading.Event() # event for the stop/ resume functionality
        self._exit_event = threading.Event()  # event for graceful exit
        self._server_changed_event = threading.Event()  # wakes the check loop after a server was added

        self.servers = {}  # address -> MonitoredServer, in display order
        self.server_rows = {}  # address -> ServerRow
        self.axes = {}  # address -> matplotlib axes
        self.recent_servers = [s for s in self.settings_data.get('recent_servers', []) if isinstance(s, str)]
        # address -> alias; kept for recent servers too, so re-adding a server restores its alias
        saved_aliases = self.settings_data.get('aliases', {})
        self.aliases = {a: n for a, n in saved_aliases.items()
                        if isinstance(a, str) and isinstance(n, str) and n.strip()} \
            if isinstance(saved_aliases, dict) else {}

        # restore the servers monitored before closing; migrate the old single 'server' setting
        if 'servers' in self.settings_data:
            addresses = [s for s in self.settings_data['servers'] if isinstance(s, str)]
        else:
            addresses = [self.settings_data.get('server', DEFAULT_SERVER)]

        self._setup_ui()
        self._apply_app_settings()
        for address in addresses:
            try:
                self._add_server(address)
            except ValueError:
                logger.warning(f"Ignoring invalid saved server '{address}'")
        self._update_recent_combo()
        self._safe_refresh()

        self.check_worker = threading.Thread(target=self._run_check_loop, daemon=True)
        self.check_worker.start()

        self.settings_worker = threading.Thread(target=self._save_settings_loop, daemon=True)
        self.settings_worker.start()

        self.update_plot_signal.connect(self._safe_refresh)

    def _apply_app_settings(self):
        """Applies loaded settings to the UI components. Must be called after _setup_ui."""
        with self.settings_data_lock:
            if 'cycle' in self.settings_data:
                self.cycle_combo.setCurrentText(self.settings_data.get('cycle', '5'))
            if 'history' in self.settings_data:
                self.history_combo.setCurrentText(self.settings_data.get('history', '1 h'))

    def _add_server(self, address: str, alias: str = None):
        """Starts monitoring a server; each server keeps its own history file.
        alias=None keeps the previously saved alias (if any).
        Raises ValueError for an invalid address. Returns False if already monitored."""
        server = MonitoredServer(address)
        if server.address in self.servers:
            return False

        if alias is not None:
            self._set_alias(server.address, alias)

        with self.check_data_lock:
            self.servers[server.address] = server

        row = ServerRow(server.address, self.aliases.get(server.address, ''),
                        self._remove_server, self._rename_server)
        self.server_rows[server.address] = row
        self.labels_layout.addWidget(row, 1)

        self._remember_recent(server.address)
        self._rebuild_axes()
        self._update_window_title()
        logger.info(f"Monitoring {server.address}")
        return True

    def _remove_server(self, address: str):
        """Stops monitoring a server; its history file is kept on disk."""
        with self.check_data_lock:
            server = self.servers.pop(address, None)
            if server is not None:
                server.save()
        if server is None:
            return

        row = self.server_rows.pop(address)
        self.labels_layout.removeWidget(row)
        row.deleteLater()

        self._rebuild_axes()
        self._update_window_title()
        self._safe_refresh()
        self._save_current_settings()
        logger.info(f"Stopped monitoring {address}")

    def _set_alias(self, address: str, alias: str):
        alias = alias.strip()
        if alias:
            self.aliases[address] = alias
        else:
            self.aliases.pop(address, None)

    def _rename_server(self, address: str):
        """Handler for the edit button of a server row."""
        alias, ok = QInputDialog.getText(
            self, "Server alias",
            f"Alias for {address} (leave empty to show only host:port):",
            text=self.aliases.get(address, ''))
        if not ok:
            return
        self._set_alias(address, alias)
        if address in self.server_rows:
            self.server_rows[address].set_alias(self.aliases.get(address, ''))
        self._save_current_settings()

    def _on_recent_selected(self, index: int):
        """Picking a recent server pre-fills its saved alias."""
        address = self.server_combo.itemText(index)
        self.alias_edit.setText(self.aliases.get(address, ''))

    def _remember_recent(self, address: str):
        # keep the most recently used servers at the top of the list
        self.recent_servers = [address] + [s for s in self.recent_servers if s != address]
        self.recent_servers = self.recent_servers[:MAX_RECENT_SERVERS]

    def _update_recent_combo(self, text: str = ''):
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        self.server_combo.addItems(self.recent_servers)
        self.server_combo.setCurrentText(text)
        self.server_combo.blockSignals(False)

    def _update_window_title(self):
        count = len(self.servers)
        suffix = f" ({count} server{'s' if count != 1 else ''})"
        self.setWindowTitle(f"Connectivity Monitor v{self.version}{suffix}")
        self.empty_label.setVisible(count == 0)

    def _on_add_server(self):
        """Handler for the Add button / Enter key in the server box."""
        text = self.server_combo.currentText().strip()
        alias = self.alias_edit.text().strip()
        try:
            # an empty alias box keeps the alias remembered for this server (if any)
            added = self._add_server(text, alias or None)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid server", str(e))
            return
        if not added:
            QMessageBox.information(self, "Already monitored",
                                    f"{text} is already being monitored.")
            return

        self._update_recent_combo()
        self.alias_edit.clear()
        self._safe_refresh()
        self._save_current_settings()
        self._server_changed_event.set()  # check the new server right away

    def _rebuild_axes(self):
        """Recreates one subplot per server, all sharing the same x axis."""
        self.fig.clear()
        self.axes = {}
        addresses = list(self.servers)
        if addresses:
            axes = self.fig.subplots(len(addresses), 1, sharex=True, squeeze=False)[:, 0]
            for address, ax in zip(addresses, axes):
                self.axes[address] = ax
        self._layout_figure()

    def _layout_figure(self):
        """Fixed margins so each subplot lines up with its label on the left."""
        height_px = max(self.canvas.height(), 1)
        width_px = max(self.canvas.width(), 1)
        bottom_px = 100  # room for the rotated date labels of the shared x axis
        left_px = 140  # room for the first rotated date label
        bottom = min(bottom_px / height_px, 0.6)
        left = min(left_px / width_px, 0.4)
        self.fig.subplots_adjust(left=left, right=0.99, top=0.98, bottom=bottom, hspace=0.15)
        # align the label column with the plot area
        top_margin = int(height_px * 0.02)
        self.labels_layout.setContentsMargins(0, top_margin, 0, int(height_px * bottom))

    def _save_current_settings(self):
        """Gathers current settings from UI and saves them."""
        current_settings = {
            'cycle': self.cycle_combo.currentText(),
            'history': self.history_combo.currentText(),
            'servers': list(self.servers),
            'recent_servers': list(self.recent_servers),
            # only keep aliases of servers that are still monitored or in the recent list
            'aliases': {a: n for a, n in self.aliases.items()
                        if a in self.servers or a in self.recent_servers}
        }
        self.io_settings_data.save_settings(current_settings)

    def _save_settings_loop(self):
        """Periodically saves settings in a background thread."""
        SETTINGS_SAVE_CYCLE_TIME_s = 120

        # sleep for a bit at the beginning to allow the ui to fully start
        sleep(2)

        while True:
            if self._exit_event.is_set():
                break

            if not self._stop_event.is_set():
                self._save_current_settings()
            for _ in range(SETTINGS_SAVE_CYCLE_TIME_s):
                sleep(1)
                if self._stop_event.is_set():
                    break

    def closeEvent(self, event):
        """Handles the window close event to ensure graceful shutdown."""
        logger.info("Closing application...")
        self._stop_event.set() # Signal all threads to stop
        self._exit_event.set() # Signal all threads to exit

        # Wait for threads to finish
        self.check_worker.join(timeout=2)
        self.settings_worker.join(timeout=2)

        self._save_current_settings()  # Perform a final save on exit
        with self.check_data_lock:
            for server in self.servers.values():
                server.save()  # Also save check_data

        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'canvas'):
            self._layout_figure()

    def _setup_ui(self):
        with open(get_running_path('version.txt')) as f:
            self.version = f.read().strip()
        self.setWindowTitle(f"Connectivity Monitor v{self.version}")
        self.setWindowIcon(QIcon(get_running_path(ICON_FILE)))
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Server (host:port):"))
        self.server_combo = QComboBox()
        self.server_combo.setEditable(True)
        self.server_combo.setInsertPolicy(QComboBox.NoInsert)
        self.server_combo.setMinimumWidth(220)
        self.server_combo.setToolTip("Type a host:port (e.g. 8.8.8.8:53) or pick a recently used server, then press Add")
        self.server_combo.lineEdit().setPlaceholderText(DEFAULT_SERVER)
        self.server_combo.lineEdit().returnPressed.connect(self._on_add_server)
        self.server_combo.activated.connect(self._on_recent_selected)
        controls.addWidget(self.server_combo)

        controls.addWidget(QLabel("Alias:"))
        self.alias_edit = QLineEdit()
        self.alias_edit.setPlaceholderText("optional, e.g. Google DNS")
        self.alias_edit.setToolTip("Optional friendly name shown next to the server's graph")
        self.alias_edit.setMinimumWidth(160)
        self.alias_edit.returnPressed.connect(self._on_add_server)
        controls.addWidget(self.alias_edit)

        self.add_server_btn = QPushButton("Add")
        self.add_server_btn.setToolTip("Start monitoring this server in a new graph")
        self.add_server_btn.clicked.connect(self._on_add_server)
        controls.addWidget(self.add_server_btn)
        controls.addSpacing(20)

        self.stop_btn = QPushButton("Stop/Resume")
        self.stop_btn.clicked.connect(self._toggle)
        controls.addWidget(self.stop_btn)

        self.pause_indicator = QLabel("●")
        self.pause_indicator.setStyleSheet("color: green; font-size: 24px;")
        controls.addWidget(self.pause_indicator)

        self.cycle_combo = QComboBox()
        self.cycle_combo.addItems(["1", "5", "10", "30"])
        controls.addWidget(QLabel("Cycle (s):"))
        controls.addWidget(self.cycle_combo)

        self.history_combo = QComboBox()
        self.history_combo.addItems(
            ["1 m", "5 m", "15 m", "30 m", "1 h", "3 h", "6 h", "12 h", "1 d", "3 d", "7 d"]
        )
        self.history_combo.currentTextChanged.connect(lambda _text: self._safe_refresh())
        controls.addWidget(QLabel("History:"))
        controls.addWidget(self.history_combo)
        controls.addStretch()

        layout.addLayout(controls)

        # plot area: a column of server labels on the left, the stacked plots on the right
        plots = QHBoxLayout()
        labels_widget = QWidget()
        labels_widget.setFixedWidth(230)
        self.labels_layout = QVBoxLayout(labels_widget)
        self.labels_layout.setSpacing(0)
        self.empty_label = QLabel("No servers monitored.\nType a host:port above and press Add.")
        self.empty_label.setWordWrap(True)
        self.labels_layout.addWidget(self.empty_label)
        plots.addWidget(labels_widget)

        self.fig = plt.figure()
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        plots.addWidget(self.canvas, 1)
        layout.addLayout(plots, 1)

        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)

    def _set_running(self):
        self.pause_indicator.setStyleSheet("color: green; font-size: 24px;")

    def _set_paused(self):
        self.pause_indicator.setStyleSheet("color: gold; font-size: 24px;")

    def _toggle(self):
        if self._stop_event.is_set():
            self._stop_event.clear()  # -> resume
            self._set_running()
        else:
            self._stop_event.set()  # -> pause
            self._set_paused()

    def _run_check_loop(self):
        # sleep for a bit at the beginning to allow the ui to fully start
        sleep(2)

        while True:
            if self._exit_event.is_set():
                break

            if not self._stop_event.is_set():
                self._refresh()
            wait = int(self.cycle_combo.currentText())
            self._server_changed_event.clear()
            for _ in range(wait):
                sleep(1)
                if self._stop_event.is_set() or self._server_changed_event.is_set():
                    break

    def _refresh(self):
        """Checks all servers in parallel, so a slow/offline server doesn't delay the others."""
        with self.check_data_lock:
            servers = list(self.servers.values())
        if not servers:
            return

        results = {}

        def check(server):
            # the (potentially slow) network check runs outside the lock so the GUI never blocks
            results[server.address] = server.checker.is_online()

        threads = [threading.Thread(target=check, args=(server,), daemon=True) for server in servers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        now = datetime.now()
        with self.check_data_lock:
            for server in servers:
                if self.servers.get(server.address) is not server:
                    continue  # the server was removed during the check, drop the stale result
                online = results.get(server.address, False)
                server.data.append({'start': now, 'end': now, 'status': int(online)})
                server.data = merge_segments(server.data)
                server.save()
                server.online = online
        self.update_plot_signal.emit()

    def _history_seconds(self):
        # convert human-readable history string -> seconds
        value, unit = self.history_combo.currentText().split()
        unit_seconds = {'m': 60, 'h': 3600, 'd': 86400}
        return int(value) * unit_seconds[unit]

    @Slot()
    def _safe_refresh(self):
        """all GUI operations now handled in main thread"""
        # Get current time for time window calculation
        now = datetime.now()
        oldest = now - timedelta(seconds=self._history_seconds())

        with self.check_data_lock:
            snapshot = [(server.address, list(server.data), server.online)
                        for server in self.servers.values()]

        last_ax = None
        for address, data, online in snapshot:
            ax = self.axes.get(address)
            if ax is None:
                continue
            last_ax = ax
            ax.clear()
            ax.set_yticks([])
            for entry in data:
                if entry['end'] < oldest:
                    continue
                ax.axvspan(max(entry['start'], oldest), entry['end'],
                           color='green' if entry['status'] else 'red', alpha=0.3)
            ax.tick_params(axis='x', labelbottom=False)
            self.server_rows[address].set_status(online)

        if last_ax is not None:
            # shared x axis: only the bottom plot shows the date labels
            last_ax.set_xlim(oldest, now)
            last_ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M:%S'))
            last_ax.tick_params(axis='x', labelbottom=True)
            # anchor the rotated labels at their tick so they are not clipped
            plt.setp(last_ax.get_xticklabels(), rotation=30, ha='right')
        self.canvas.draw_idle()

        if not self._stop_event.is_set():
            self._set_running()


if __name__ == '__main__':
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(get_running_path(ICON_FILE)))
    window = MainWindow()
    window.resize(1200, 700)
    window.show()
    try:
        app.exec()
    except:
        logger.error(f'An error occurred while running the app\n'
                     f'{format_exc(chain=False)}')
        sys.exit(1)
