# pyside6

## app basics
from PySide6.QtWidgets import QApplication, QWidget   # Common starting imports
app = QApplication(sys.argv)              # Create the Qt application (one per process)
window = QWidget()                        # Create a basic window/widget
window.show()                             # Show a widget on screen
sys.exit(app.exec())                      # Start the Qt event loop

## widgets
QLabel("Hello")                           # A text label
QPushButton("Click me")                   # A clickable button
QLineEdit()                               # A single-line text input
QTextEdit()                               # A multi-line text input
QCheckBox("Enable")                       # A checkbox
QComboBox()                               # A dropdown list
QListWidget()                             # A simple list of items
QTableWidget(rows, cols)                  # A basic spreadsheet-like grid

## layouts
QVBoxLayout()                             # Stack widgets vertically
QHBoxLayout()                             # Arrange widgets horizontally
QGridLayout()                             # Arrange widgets in a grid
layout.addWidget(widget)                  # Add a widget to a layout
layout.addStretch()                       # Add flexible empty space
widget.setLayout(layout)                  # Apply a layout to a widget

## signals & slots
button.clicked.connect(on_click)          # Connect a built-in signal to a handler
Signal(int)                               # Define a custom signal on a QObject subclass
signal.emit(value)                        # Manually emit a custom signal
signal.disconnect(handler)                # Disconnect a signal from a handler
@Slot()                                   # Mark a method as an explicit slot

## windows
class MainWindow(QMainWindow):            # Subclass for a full application window
self.setWindowTitle("My App")             # Set the window title
self.setCentralWidget(widget)             # Set the window's main content widget
self.resize(800, 600)                     # Resize the window
self.setWindowIcon(QIcon("icon.png"))     # Set the window/taskbar icon
QMessageBox.information(self, "Title", "Message")  # Show an info dialog
QFileDialog.getOpenFileName(self)         # Show an "open file" dialog

## threads
QThread()                                 # Run work off the main/UI thread
worker.moveToThread(thread)               # Move a QObject worker onto a thread
thread.started.connect(worker.run)        # Start the worker when the thread starts
QTimer.singleShot(1000, callback)         # Run a callback once after a delay (ms)

## styling
widget.setStyleSheet("background: #222;") # Apply Qt-flavored CSS-like styling
QIcon("icon.png")                         # Load an icon for a window/widget/action
QFont("Arial", 12)                        # Create a font
widget.setFixedSize(400, 300)             # Lock a widget's size
