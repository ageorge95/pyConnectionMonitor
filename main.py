import sys
import threading
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from socket import gethostbyname, create_connection
from pickle import dump, load
from os import path
from time import sleep
from logging import getLogger
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QSizePolicy
)
from ag95 import configure_logger

# Configure logging
configure_logger(log_level='INFO')
logger = getLogger(__name__)

class IOHandler:
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
        if path.exists(f"{self.server}.pkl"):
            try:
                with open(f"{self.server}.pkl", 'rb') as f:
                    return load(f)
            except Exception as e:
                self.log.error(f"Failed loading: {e}")
        return []

class InternetChecker:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = getLogger(self.__class__.__name__)

    def is_online(self) -> bool:
        try:
            gethostbyname(self.host)
        except Exception:
            self.log.warning(f"DNS failed for {self.host}")
            return False
        try:
            conn = create_connection((self.host, self.port), timeout=self.timeout)
            conn.close()
            return True
        except Exception:
            self.log.warning(f"Cannot connect to {self.host}:{self.port}")
            return False

class MainWindow(QMainWindow):
    def __init__(self, address: str):
        super().__init__()
        self.address = address
        self.host, port = address.split(':')
        self.port = int(port)

        self.io = IOHandler(address)
        self.data = self.io.load()

        self.checker = InternetChecker(self.host, self.port)
        self._setup_ui()
        self._plot_initial()

        self._stop_event = threading.Event()
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()

    def _setup_ui(self):
        self.setWindowTitle(f"Connectivity Monitor ({self.address})")
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
        for entry in self.data:
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

    def _run_loop(self):
        # sleep for a bit at the begining to allow the ui to fully start
        sleep(2)
        self._set_running()  # initial state

        while True:
            if not self._stop_event.is_set():
                self._refresh()
            wait = int(self.cycle_combo.currentText())
            for _ in range(wait):
                sleep(1)
                if self._stop_event.is_set():
                    break

    def _refresh(self):
        online = self.checker.is_online()
        now = datetime.now()
        self.data.append({'start': now, 'end': now, 'status': int(online)})

        # convert human-readable history string -> seconds
        history_text = self.history_combo.currentText()
        value, unit = history_text.split()
        value = int(value)
        unit_seconds = {'m': 60, 'h': 3600, 'd': 86400}
        seconds = value * unit_seconds[unit]
        oldest = now - timedelta(seconds=seconds)

        # decimate data to show color spans instead of lines
        MAX_GAP = timedelta(seconds=35)

        if self.data:
            merged = [self.data[0]]
            for seg in self.data[1:]:
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
            self.data = merged

        # Redraw
        self.ax.clear()
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M:%S'))
        self.ax.tick_params(axis='x', rotation=45)
        for entry in self.data:
            start, end, status = entry['start'], entry['end'], entry['status']
            if end < oldest:
                continue
            self.ax.axvspan(max(start, oldest), end,
                            color='green' if status else 'red', alpha=0.3)
        self.ax.set_xlim(oldest, now)
        self.canvas.draw()

        self.io.save(self.data)
        self.status_label.setText(f"Status: {'ONLINE' if online else 'OFFLINE'}")
        self._set_running()  # ensure green after each probe

if __name__ == '__main__':
    app = QApplication(sys.argv)
    address = sys.argv[1] if len(sys.argv) > 1 else '8.8.8.8:53'
    window = MainWindow(address)
    window.resize(1200, 600)
    window.show()
    sys.exit(app.exec())
