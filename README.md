# pyConnectionMonitor
Simple python application that checks network connectivity to another computer:port.

## Highlights:
- the app is able to monitor any computer host and port, or use the default 8.8.8.8:53 to check for internet connectivity
- multiple servers can be monitored at the same time: type a host:port in the GUI and press Add; each server gets its own graph, stacked one below the other on a shared time axis
- each server can have an optional alias (e.g. "Google DNS"), entered when adding it or changed later with the ✎ button; aliases are remembered
- each server's alias, host:port and ONLINE/OFFLINE status are shown next to its graph; the ✕ button stops monitoring it
- all servers are checked in parallel, so a slow or offline server does not delay the others
- the monitored servers are restored when the app is started again; recently used servers are remembered and each server keeps its own history
- the app shows a graph to easily see the connection status over time
- you can select the cycle time, default 1 second
- you can select the plot history size, default 1 minute
- you can pause/ resume the check at any time
- the check status is saved on disk so you will not lose past connection status data when the app is closed

# GUI layout

![](ReadMe_res/GUI_layout.png)

# Support
Found this project useful? Send your ❤ in any form you can 🙂. Please contact me if you donated and want to be added to the contributors list !

- chia XCH---xch1glz7ufrfw9xfp5rnlxxh9mt9vk9yc8yjseet5c6u0mmykq8cpseqna6494
