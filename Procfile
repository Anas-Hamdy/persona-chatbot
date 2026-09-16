# Single worker by default: chat history is kept in an in-memory dict per
# process (see DEPLOYMENT_CHECKLIST.md), so running more than one worker
# means different requests from the same user can hit different workers
# with different history/profile state. Raise --workers only after moving
# history and profile storage to a shared store (Redis/DB).
web: gunicorn --chdir src "app:app" --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60
