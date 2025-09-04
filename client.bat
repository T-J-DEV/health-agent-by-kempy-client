@echo off
:: Check if Python is installed
where python >nul 2>&1
if %errorlevel%==0 (
    echo Python is already installed.
    python --version
) else (
    echo Python not found. Installing...
    
    :: Download Python installer (adjust version as needed)
    set "PYTHON_URL=https://www.python.org/ftp/python/3.12.5/python-3.12.5-amd64.exe"
    set "PYTHON_INSTALLER=%TEMP%\python-installer.exe"

    powershell -Command "Invoke-WebRequest -Uri %PYTHON_URL% -OutFile %PYTHON_INSTALLER%"

    :: Run installer silently (install for all users, add to PATH)
    %PYTHON_INSTALLER% /quiet InstallAllUsers=1 PrependPath=1 Include_test=0

    :: Cleanup
    del %PYTHON_INSTALLER%

    echo Python installed successfully.
    python --version
)

python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r health-agent-by-kempy/agent/requirements.txt
copy health-agent-by-kempy\agent\.env.example health-agent-by-kempy\agent\.env

echo Starting agent in continuous mode (30s interval)...
echo Press Ctrl+C to stop
python health-agent-by-kempy/agent/agent.py --interval 30
