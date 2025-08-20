import sys
import os
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
    QPushButton, QComboBox, QSizePolicy
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Signal, Slot
from ag95 import configure_logger

# Configure logging
configure_logger(log_level='INFO')
logger = getLogger(__name__)

SETTINGS_FILE = "settings.json"

def get_running_path(relative_path):
    if '_internal' in os.listdir():
        return os.path.join('_internal', relative_path)
    else:
        return relative_path

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

class MainWindow(QMainWindow):
    update_plot_signal = Signal()

    def __init__(self, address: str):
        super().__init__()
        self.address = address
        self.host, port = address.split(':')
        self.port = int(port)

        self.io_check_data = IOHandlerCheckData(address)
        self.io_settings_data = IOHandlerSettingsData

        self.check_data = self.io_check_data.load()
        self.settings_data = self.io_settings_data.load_settings()

        self.check_data_lock = threading.Lock()
        self.settings_data_lock = threading.Lock()

        self.checker = InternetChecker(self.host, self.port)
        self._setup_ui()
        self._apply_app_settings()
        self._plot_initial()

        self._stop_event = threading.Event() # event for the stop/ resume functionality
        self._exit_event = threading.Event()  # event for graceful exit

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

    def _save_current_settings(self):
        """Gathers current settings from UI and saves them."""
        current_settings = {
            'cycle': self.cycle_combo.currentText(),
            'history': self.history_combo.currentText()
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
        self.io_check_data.save(self.data)  # Also save check_data

        event.accept()

    def _setup_ui(self):
        self.setWindowTitle(f"Connectivity Monitor v{open(get_running_path('version.txt')).read()} ({self.address})")
        self.setWindowIcon(QIcon(get_running_path('icon.ico')))
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        controls = QHBoxLayout()
        self.status_label = QLabel("Status: INITIALIZED")
        controls.addWidget(self.status_label)

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
        controls.addWidget(QLabel("History:"))
        controls.addWidget(self.history_combo)

        layout.addLayout(controls)

        self.fig, self.ax = plt.subplots()
        self.fig.subplots_adjust(bottom=0.25)  # leave room for rotated labels
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M:%S'))
        self.ax.tick_params(axis='x', rotation=45)

        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.canvas)

        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)

    def _set_running(self):
        self.pause_indicator.setStyleSheet("color: green; font-size: 24px;")

    def _set_paused(self):
        self.pause_indicator.setStyleSheet("color: gold; font-size: 24px;")

    def _plot_initial(self):
        with self.check_data_lock:
            for entry in self.check_data:
                start = entry['start']
                end = entry['end']
                status = entry['status']
                self.ax.axvspan(start, end,
                                color='green' if status else 'red', alpha=0.3)
        self.fig.tight_layout()
        self.canvas.draw()

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
        self._set_running()  # initial state

        while True:
            if self._exit_event.is_set():
                break

            if not self._stop_event.is_set():
                self._refresh()
            wait = int(self.cycle_combo.currentText())
            for _ in range(wait):
                sleep(1)
                if self._stop_event.is_set():
                    break

    def _refresh(self):
        with self.check_data_lock:
            online = self.checker.is_online()
            now = datetime.now()
            self.check_data.append({'start': now, 'end': now, 'status': int(online)})

            # convert human-readable history string -> seconds
            history_text = self.history_combo.currentText()
            value, unit = history_text.split()
            value = int(value)
            unit_seconds = {'m': 60, 'h': 3600, 'd': 86400}
            seconds = value * unit_seconds[unit]
            oldest = now - timedelta(seconds=seconds)

            # decimate data to show color spans instead of lines
            MAX_GAP = timedelta(seconds=35)

            if self.check_data:
                merged = [self.check_data[0]]
                for seg in self.check_data[1:]:
                    last = merged[-1]
                    gap = seg['start'] - last['end']
                    if seg['status'] == last['status'] and gap <= MAX_GAP:
                        # contiguous & same colour → extend
                        last['end'] = seg['end']
                    else:
                        # gap too large or colour change → new segment
                        merged.append({'start': seg['start'],
                                       'end': seg['end'],
                                       'status': seg['status']})

            # Data pruning logic
            max_history = 7 * 86400  # Base retention
            buffer_window = 300  # Keep extra 5 minutes for merging context
            cutoff = datetime.now() - timedelta(seconds=max_history + buffer_window)

            # Only prune entries that are fully outside buffer window
            self.data = [entry for entry in merged
                         if entry['end'] > cutoff or
                         (entry['end'] - entry['start']) > timedelta(seconds=300)]

            self.io_check_data.save(self.data)
        self.current_online = online
        self.update_plot_signal.emit()

    @Slot()
    def _safe_refresh(self):
        """all GUI operations now handled in main thread"""
        # Get current time for time window calculation
        now = datetime.now()

        # Recalculate history window
        history_text = self.history_combo.currentText()
        value, unit = history_text.split()
        value = int(value)
        unit_seconds = {'m': 60, 'h': 3600, 'd': 86400}
        seconds = value * unit_seconds[unit]
        oldest = now - timedelta(seconds=seconds)

        # Update plot
        self.ax.clear()
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M:%S'))
        self.ax.tick_params(axis='x', rotation=45)

        with self.check_data_lock:
            for entry in self.data:
                start = entry['start']
                end = entry['end']
                status = entry['status']
                if end < oldest:
                    continue
                self.ax.axvspan(max(start, oldest), end,
                                color='green' if status else 'red', alpha=0.3)

        self.ax.set_xlim(oldest, now)
        self.canvas.draw()

        # Update status label
        self.status_label.setText(f"Status: {'ONLINE' if self.current_online else 'OFFLINE'}")
        self._set_running()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    address = sys.argv[1] if len(sys.argv) > 1 else '8.8.8.8:53'
    window = MainWindow(address)
    window.resize(1200, 600)
    window.show()
    try:
        app.exec()
    except:
        logger.error(f'An error occurred while running the app\n'
                     f'{format_exc(chain=False)}')
        sys.exit(1)