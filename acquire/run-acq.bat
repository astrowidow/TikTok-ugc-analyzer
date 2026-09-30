@echo off
REM UGC Analyzer acquisition worker (acquire\worker.py). Started by scheduled task tiktok-acq; exits when the queue is empty.
REM Logs: logs\acq-worker.log and each analysis fetch_log\. Stdout goes to NUL on purpose: an orphan chromedriver once held
REM the redirected file open and the next start failed silently (COMMENT_ACQUISITION_HANDOVER 7-D).
set PYTHONUTF8=1
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m acquire.worker > NUL 2>&1
