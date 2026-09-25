import os

# Tests never ask GitHub for the latest release; test_updates.py turns it on where needed.
os.environ["EXCEL_BRIDGE_UPDATE_CHECK"] = "0"
