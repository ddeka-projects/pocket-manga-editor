Pocket Manga Editor - Windows Portable Build
=============================================

Launch the application with:

    Pocket Manga Editor.exe

Keep the executable and the _internal folder together. The executable will not
work if it is copied out of this portable folder by itself.

The application stores its settings and paired-phone verifier in the current
Windows user's local application-data directory. Manga sources and the
.pocket-manga-editor workspace remain in the working folder chosen in the app.

For automatic launch when the PC starts, point Windows Task Scheduler at the
full path to Pocket Manga Editor.exe. Move this entire portable folder to its
permanent location before creating that scheduled task.

This is an unsigned local build. Windows may show a SmartScreen prompt when the
application came from a downloaded ZIP.
