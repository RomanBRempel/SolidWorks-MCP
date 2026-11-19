"""COM on the current thread for the length of a block, left exactly as it was found.

pythoncom.CoInitialize() and pythoncom.CoUninitialize() are NOT a balanced pair
on the thread that imported pythoncom first. pywin32 remembers that thread as
initialised: CoInitialize is a no-op there, CoUninitialize really
uninitialises. A helper that brackets its work with the two tears that
thread's apartment down, and every COM object the thread holds answers
0x800401FD, 'object is not connected to server', from then on - the SolidWorks
application object and every document with it.

Found live on 2026-11-19: reading a message box through MSAA
(dialogs.drawn_text) on the thread that held the SolidWorks session killed the
session. The server was spared only because its session lives on the COM
worker thread while dialogs are read on the event loop and on pool threads; a
script, a test or a job that does both on one thread was not.

ole32 counts honestly: every CoInitializeEx that answers S_OK or S_FALSE is
owed one CoUninitialize, on any thread.
"""

import contextlib
import ctypes
import os

COINIT_APARTMENTTHREADED = 2
S_OK, S_FALSE = 0, 1
APTTYPEQUALIFIER_IMPLICIT_MTA = 1


@contextlib.contextmanager
def com():
    """COM is usable inside the block; afterwards the thread is as it was before.

    A thread that had COM keeps it (the count goes up and down again); a thread
    that had none gets an apartment for the block. A thread in the other
    threading model (RPC_E_CHANGED_MODE) already has COM: nothing is taken and
    nothing is given back. Release the COM objects made inside before leaving
    the block - do the work in a function called from it."""
    if os.name != "nt":
        yield
        return
    ole32 = ctypes.windll.ole32
    counted = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) in (S_OK, S_FALSE)
    try:
        yield
    finally:
        if counted:
            ole32.CoUninitialize()


def initialised() -> bool:
    """Has the current thread initialised COM? False off Windows.

    CoGetApartmentType also answers for a thread that never did, once some other thread of
    the process made a multi-threaded apartment: such a thread sits in the 'implicit MTA'
    (qualifier 1), which goes away with the threads that made it - not initialised."""
    if os.name != "nt":
        return False
    kind, qualifier = ctypes.c_int(), ctypes.c_int()
    answered = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(kind), ctypes.byref(qualifier)) == 0
    return answered and qualifier.value != APTTYPEQUALIFIER_IMPLICIT_MTA
