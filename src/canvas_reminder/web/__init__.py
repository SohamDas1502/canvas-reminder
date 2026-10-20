"""Multi-user web app: per-user settings, encryption, the API, and the scheduled workers.

The reminder engine itself (ics, diff, pipeline, notifiers) is shared with the
single-user deployment; this package only adds what having many users requires.
"""
