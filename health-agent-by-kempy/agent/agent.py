
import os
import time
import json
import shutil
import subprocess
import argparse
import psutil
import requests
import threading
import enum
import sys
import zipfile
import tempfile
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime

# Agent version - increment this when updating
AGENT_VERSION = "1.0.0"

class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8000")
DEVICE_KEY = os.getenv("DEVICE_KEY", "CHANGE_ME_DEVICE_KEY")
DEVICE_NAME = os.getenv("DEVICE_NAME", "New Device")

HEADERS = {"X-Device-Key": DEVICE_KEY}

def win():
    return os.name == "nt"

def run(cmd):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except subprocess.CalledProcessError as e:
        return e.output.strip()

def get_gpu_info():
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            gpu = gpus[0]  # Get first GPU
            return gpu.name, gpu.load * 100, gpu.temperature
    except Exception:
        # Try Nvidia via nvidia-smi as fallback
        if win():
            try:
                q = run('nvidia-smi --query-gpu=name,utilization.gpu,temperature.gpu --format=csv,noheader,nounits')
                if q:
                    parts = [p.strip() for p in q.split(",")]
                    name = parts[0]
                    util = float(parts[1]) if len(parts) > 1 else 0.0
                    temp = float(parts[2]) if len(parts) > 2 else 0.0
                    return name, util, temp
            except Exception:
                pass
    # Fallbacks
    return "Unknown", 0.0, 0.0

def get_net_speeds_mbps():
    """Get network speeds using speedtest-cli or psutil as fallback"""
    try:
        import speedtest
        s = speedtest.Speedtest()
        s.get_best_server()
        down = s.download() / 1_000_000  # Convert to Mbps
        up = s.upload() / 1_000_000  # Convert to Mbps
        return round(up, 2), round(down, 2)
    except Exception:
        # Fallback to psutil
        c1 = psutil.net_io_counters()
        time.sleep(1.0)
        c2 = psutil.net_io_counters()
        up = (c2.bytes_sent - c1.bytes_sent) * 8 / 1_000_000
        down = (c2.bytes_recv - c1.bytes_recv) * 8 / 1_000_000
        return round(up, 2), round(down, 2)

def collect_metrics():
    # CPU metrics with per-core info
    cpu_overall = psutil.cpu_percent(interval=0.5)
    cpu_per_core = psutil.cpu_percent(interval=0.1, percpu=True)
    cpu_freq = psutil.cpu_freq()

    # Memory metrics
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk metrics for all mounted drives
    disk_metrics = {}
    for part in psutil.disk_partitions(all=False):
        if part.fstype:  # Skip empty drives
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disk_metrics[part.mountpoint] = {
                    "total_gb": usage.total / (1024**3),
                    "used_percent": usage.percent
                }
            except Exception:
                continue

    # GPU metrics
    gpu_name, gpu_util, gpu_temp = get_gpu_info()

    # Network speeds - get average over a short period
    up, down = get_net_speeds_mbps()

    # Enhanced process metrics
    top_processes = []
    try:
        processes = list(psutil.process_iter(["name", "cpu_percent", "memory_percent"]))
        processes.sort(key=lambda p: p.info.get("cpu_percent", 0), reverse=True)
        for proc in processes[:5]:  # Get top 5 CPU-consuming processes
            try:
                top_processes.append({
                    "name": proc.info["name"],
                    "cpu_percent": proc.info["cpu_percent"],
                    "memory_percent": proc.info.get("memory_percent", 0)
                })
            except Exception:
                continue
    except Exception:
        pass

    # Get main disk usage (C: on Windows, / on Linux)
    main_disk = psutil.disk_usage("C:\\" if win() else "/")

    extra = {
        "num_processes": len(psutil.pids()),
        "top_processes": top_processes,
        "cpu_info": {
            "per_core": cpu_per_core,
            "frequency_mhz": cpu_freq.current if cpu_freq else 0,
            "max_frequency_mhz": cpu_freq.max if cpu_freq else 0
        },
        "memory_info": {
            "total_gb": vm.total / (1024**3),
            "available_gb": vm.available / (1024**3),
            "swap_used_percent": swap.percent
        },
        "disk_info": disk_metrics,
        "system": {
            "os": "windows" if win() else "linux",
            "boot_time": psutil.boot_time()
        }
    }

    payload = {
        "cpu_percent": round(cpu_overall, 2),
        "ram_percent": round(vm.percent, 2),
        "gpu_name": gpu_name,
        "gpu_util": round(gpu_util, 2),
        "gpu_temp": round(gpu_temp, 2),
        "disk_usage_percent": round(main_disk.percent, 2),
        "net_up_mbps": up,
        "net_down_mbps": down,
        "extra": extra,
    }

    payload = {
        "cpu_percent": round(cpu_overall, 2),
        "ram_percent": round(vm.percent, 2),
        "gpu_name": gpu_name,
        "gpu_util": round(gpu_util, 2),
        "gpu_temp": round(gpu_temp, 2),
        "disk_usage_percent": round(main_disk.percent, 2),
        "net_up_mbps": up,
        "net_down_mbps": down,
        "extra": extra,
    }
    return payload
    return payload

# -------- Task Management --------
class TaskManager:
    def __init__(self):
        self.current_task = None
        self.stop_flag = False
        self.thread = None

    def start(self):
        self.stop_flag = False
        self.thread = threading.Thread(target=self._task_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        if self.thread:
            self.thread.join()

    def _task_loop(self):
        while not self.stop_flag:
            try:
                # Check for queued tasks
                response = requests.get(
                    f"{SERVER_URL}/api/device/me",
                    headers=HEADERS
                )
                device_info = response.json()
                device_id = device_info["id"]

                response = requests.get(
                    f"{SERVER_URL}/api/device/{device_id}/tasks?status=queued",
                    headers=HEADERS
                )
                
                if response.status_code == 200:
                    tasks = response.json()
                    for task in tasks:
                        if self.stop_flag:
                            break
                        
                        # Update task to running
                        self.current_task = task
                        requests.put(
                            f"{SERVER_URL}/api/device/{device_id}/tasks/{task['id']}",
                            headers=HEADERS,
                            json={"status": "running"}
                        )

                        # Execute task
                        try:
                            result = self._execute_task(task)
                            status = "completed"
                        except Exception as e:
                            result = f"Error: {str(e)}"
                            status = "failed"

                        # Update task with result
                        requests.put(
                            f"{SERVER_URL}/api/device/{device_id}/tasks/{task['id']}",
                            headers=HEADERS,
                            json={"status": status, "result": result}
                        )

                        self.current_task = None

            except Exception as e:
                print(f"Task loop error: {e}")

            # Wait before checking again
            for _ in range(30):  # 30 second interval
                if self.stop_flag:
                    break
                time.sleep(1)

    def _execute_task(self, task):
        task_type = task["type"]
        
        if task_type == "disk_cleanup":
            return self._disk_cleanup()
        elif task_type == "memory_optimization":
            return self._memory_optimization()
        elif task_type == "network_diagnosis":
            return self._network_diagnosis()
        elif task_type == "flush_dns":
            return flush_dns()
        elif task_type == "clear_temp":
            return clear_temp()
        elif task_type == "clear_shader_cache":
            return clear_shader_cache()
        else:
            raise ValueError(f"Unknown task type: {task_type}")



# -------- Maintenance Tasks (safe) --------
def disk_cleanup():
    """Comprehensive disk cleanup task"""
    results = []
    
    # Get initial disk space
    disk = psutil.disk_usage("C:\\") if win() else psutil.disk_usage("/")
    space_before = disk.used / (1024**3)  # Convert to GB
    
    # Clear temp files
    temp_result = clear_temp()
    results.append(f"Temp cleanup: {temp_result}")
    
    # Clear Windows Update cache if on Windows
    if win():
        try:
            update_path = os.path.expandvars("%SystemRoot%\\SoftwareDistribution\\Download")
            if os.path.exists(update_path):
                size_before = sum(os.path.getsize(os.path.join(dirpath, f))
                    for dirpath, _, filenames in os.walk(update_path)
                    for f in filenames) / (1024**3)  # Convert to GB
                shutil.rmtree(update_path, ignore_errors=True)
                results.append(f"Cleared Windows Update cache ({size_before:.2f} GB)")
        except Exception as e:
            results.append(f"Windows Update cleanup error: {e}")

    # Calculate total space saved
    disk = psutil.disk_usage("C:\\") if win() else psutil.disk_usage("/")
    space_after = disk.used / (1024**3)
    space_saved = space_before - space_after
    results.append(f"\nTotal space saved: {space_saved:.2f} GB")
    results.append(f"Disk usage: {disk.percent}%")

    return "\n".join(results)

def memory_optimization():
    """Memory optimization task"""
    results = []
    
    # Get initial memory state
    mem = psutil.virtual_memory()
    before_used = mem.used / (1024**3)  # Convert to GB
    before_percent = mem.percent
    
    if win():
        # Get top memory-using processes
        processes = []
        for proc in psutil.process_iter(['name', 'memory_percent']):
            try:
                pinfo = proc.info
                if pinfo['memory_percent'] > 1.0:  # Only processes using >1% memory
                    processes.append((pinfo['name'], pinfo['memory_percent']))
            except Exception:
                pass
        
        if processes:
            processes.sort(key=lambda x: x[1], reverse=True)
            results.append("Top memory-using processes before optimization:")
            for name, mem_percent in processes[:3]:
                results.append(f"  {name}: {mem_percent:.1f}%")
            results.append("")
        
        # Empty working set of known memory-heavy processes
        memory_heavy = ['chrome.exe', 'firefox.exe', 'msedge.exe', 'brave.exe']
        for proc in memory_heavy:
            run(f"wmic process where name='{proc}' call EmptyWorkingSet")
        results.append("Optimized browser memory usage")
        
        # Clear standby list
        run("PowerShell -Command \"Clear-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management' -Force\"")
        results.append("Cleared system standby list")

    # Force a garbage collection in Python
    import gc
    gc.collect()
    
    # Get final memory state
    mem = psutil.virtual_memory()
    after_used = mem.used / (1024**3)
    after_percent = mem.percent
    
    memory_freed = before_used - after_used
    results.append(f"\nMemory freed: {memory_freed:.2f} GB")
    results.append(f"Memory usage: {before_percent}% → {after_percent}%")
    
    if memory_freed < 0.1:
        results.append("\nNote: Limited memory could be freed. Consider:")
        results.append("- Closing unused applications")
        results.append("- Restarting memory-intensive programs")
        results.append("- Checking for memory leaks")
    
    return "\n".join(results)

def network_diagnosis():
    """Network diagnosis task"""
    results = []
    
    # Test DNS resolution
    dns_servers = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    results.append("DNS Resolution Test:")
    for server in dns_servers:
        try:
            start = time.time()
            response = requests.get(f"https://{server}", timeout=5)
            latency = (time.time() - start) * 1000
            results.append(f"  {server}: OK ({latency:.1f}ms)")
        except Exception as e:
            results.append(f"  {server}: Failed ({str(e)})")
    
    # Speed test
    results.append("\nNetwork Speed Test:")
    try:
        up, down = get_net_speeds_mbps()
        quality = "Excellent" if min(up, down) > 100 else "Good" if min(up, down) > 25 else "Fair" if min(up, down) > 10 else "Poor"
        results.append(f"  Upload: {up:.1f} Mbps")
        results.append(f"  Download: {down:.1f} Mbps")
        results.append(f"  Connection Quality: {quality}")
    except Exception as e:
        results.append(f"  Speed test error: {e}")
    
    if win():
        # Get network adapter info
        results.append("\nNetwork Adapters:")
        netsh = run("netsh interface ipv4 show interfaces")
        for line in netsh.splitlines():
            if line.strip():  # Skip empty lines
                results.append(f"  {line.strip()}")
        
        # Get active connections
        results.append("\nActive Connections:")
        try:
            connections = psutil.net_connections()
            established = sum(1 for conn in connections if conn.status == 'ESTABLISHED')
            listening = sum(1 for conn in connections if conn.status == 'LISTEN')
            results.append(f"  Established: {established}")
            results.append(f"  Listening: {listening}")
        except Exception:
            results.append("  Could not retrieve connection information")
    
    return "\n".join(results)

def flush_dns():
    if not win():
        return "DNS flush implemented for Windows only"
    out = run("ipconfig /flushdns")
    return out or "Flushed DNS"

def clear_temp():
    if not win():
        return "Clear temp implemented for Windows only"
    cleared = 0
    for path in [os.environ.get("TEMP"), os.environ.get("TMP"), "C:\\Windows\\Temp"]:
        if not path: 
            continue
        try:
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        os.remove(fp)
                        cleared += 1
                    except Exception:
                        pass
        except Exception:
            pass
    return f"Cleared ~{cleared} temp files"

def clear_shader_cache():
    if not win():
        return "Shader cache clear implemented for Windows only"
    # Common shader cache directories. Adjust as needed.
    paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\NVIDIA\DXCache"),
        os.path.expandvars(r"%LOCALAPPDATA%\D3DSCache"),
        os.path.expandvars(r"%LOCALAPPDATA%\NVIDIA\GLCache"),
    ]
    cleared = 0
    for p in paths:
        if p and os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                for f in files:
                    try:
                        os.remove(os.path.join(root, f))
                        cleared += 1
                    except Exception:
                        pass
    return f"Cleared ~{cleared} shader cache files"

TASKS = {
    "disk_cleanup": disk_cleanup,
    "memory_optimization": memory_optimization,
    "network_diagnosis": network_diagnosis,
    "flush_dns": flush_dns,
    "clear_temp": clear_temp,
    "clear_shader_cache": clear_shader_cache,
}

def process_task_queue():
    """Poll for pending tasks and execute them"""
    try:
        # Get device ID first if we don't have it
        if not hasattr(process_task_queue, "device_id"):
            r = requests.get(f"{SERVER_URL}/api/device/me", headers=HEADERS)
            if r.status_code == 200:
                process_task_queue.device_id = r.json()["id"]
                print(f"✓ Got device ID: {process_task_queue.device_id}")
            else:
                print(f"Failed to get device ID: {r.status_code}")
                return
        
        # Get pending tasks
        r = requests.get(
            f"{SERVER_URL}/api/device/{process_task_queue.device_id}/tasks?status=queued",
            headers=HEADERS
        )
        if r.status_code != 200:
            print(f"Failed to get tasks: {r.status_code}")
            return
            
        tasks = r.json()
        if tasks:
            print(f"Found {len(tasks)} queued tasks")
            
        for task in tasks:
            task_id = task["id"]
            task_type = task["type"]
            
            if task_type not in TASKS:
                print(f"⨯ Unknown task type: {task_type}")
                continue
                
            print(f"▶️ Starting task {task_id}: {task_type}")
            
            # Mark as running
            try:
                r = requests.put(
                    f"{SERVER_URL}/api/device/{process_task_queue.device_id}/tasks/{task_id}",
                    json={"status": TaskStatus.RUNNING.value},
                    headers=HEADERS
                )
                r.raise_for_status()
            except Exception as e:
                print(f"⨯ Failed to mark task {task_id} as running: {e}")
                continue
            
            try:
                # Run the task
                output = TASKS[task_type]()
                status = TaskStatus.COMPLETED.value
                print(f"✓ Task {task_id} completed successfully:")
                for line in output.splitlines():
                    print(f"  {line}")
            except Exception as e:
                output = str(e)
                status = TaskStatus.FAILED.value
                print(f"⨯ Task {task_id} failed: {e}")
                
            # Update task status
            try:
                r = requests.put(
                    f"{SERVER_URL}/api/device/{process_task_queue.device_id}/tasks/{task_id}",
                    json={"status": status, "result": output},
                    headers=HEADERS
                )
                r.raise_for_status()
            except Exception as e:
                print(f"⨯ Failed to update task {task_id} status: {e}")
            
    except Exception as e:
        print(f"Error processing tasks: {e}")

def ensure_device_registered():
    """Make sure we have a valid device key and are registered with the server"""
    global DEVICE_KEY, HEADERS
    
    if DEVICE_KEY == "CHANGE_ME_DEVICE_KEY":
        print("ℹ️ First run - registering device...")
        try:
            r = requests.post(f"{SERVER_URL}/api/register", data={"name": DEVICE_NAME})
            if r.status_code == 200:
                device = r.json()
                DEVICE_KEY = device["device_key"]
                HEADERS["X-Device-Key"] = DEVICE_KEY
                
                # Save to .env file
                env_file = Path(__file__).with_name(".env")
                if env_file.exists():
                    content = env_file.read_text()
                    content = content.replace("CHANGE_ME_DEVICE_KEY", DEVICE_KEY)
                    env_file.write_text(content)
                print(f"✓ Registered as '{DEVICE_NAME}' (key: {DEVICE_KEY[:8]}...)")
            else:
                raise Exception(f"Registration failed: {r.status_code} {r.text}")
        except Exception as e:
            print(f"⨯ Registration failed: {e}")
            return False
    return True

def send_metrics_with_backoff(initial_delay=1, max_delay=300):
    """Send metrics with exponential backoff on failure"""
    delay = initial_delay
    device_id = None
    
    # Ensure we're registered
    if not ensure_device_registered():
        return
    
    while True:
        try:
            # Get device ID on first run
            if device_id is None:
                r = requests.get(f"{SERVER_URL}/api/device/me", headers=HEADERS)
                if r.status_code == 200:
                    device_id = r.json()["id"]
                    print(f"✓ Connected as device #{device_id}")
                else:
                    raise Exception(f"Failed to get device ID: {r.status_code}")
            
            # Collect and send metrics
            payload = collect_metrics()
            r = requests.post(
                f"{SERVER_URL}/api/device/{device_id}/metrics", 
                headers=HEADERS, 
                json=payload,
                timeout=10
            )
            
            if r.status_code == 200:
                print(f"✓ Metrics sent ({r.json()['id']})")
                delay = initial_delay  # Reset delay on success
            else:
                raise Exception(f"HTTP {r.status_code}: {r.text}")
                
        except Exception as e:
            print(f"⨯ Error sending metrics: {e}")
            delay = min(delay * 2, max_delay)  # Exponential backoff
            
        # Process any pending tasks
        process_task_queue()
        
        time.sleep(delay)

def main():
    parser = argparse.ArgumentParser(description="PC Health Maintainer Agent")
    parser.add_argument("--once", action="store_true", help="Collect & send a single metric")
    parser.add_argument("--interval", type=int, default=60, help="Collect every N seconds (default: 60)")
    parser.add_argument("--task", choices=list(TASKS.keys()), help="Run a single maintenance task locally (no server)")
    args = parser.parse_args()

    # Print startup info
    print(f"PC Health Maintainer Agent")
    print(f"Server: {SERVER_URL}")
    print(f"Device: {DEVICE_NAME}")
    
    if args.task:
        print(TASKS[args.task]())
        return

    try:
        if args.once:
            # Ensure we're registered even for one-time runs
            if not ensure_device_registered():
                return 1
                
            # Get device ID
            r = requests.get(f"{SERVER_URL}/api/device/me", headers=HEADERS)
            if r.status_code != 200:
                print(f"Failed to get device ID: HTTP {r.status_code}")
                return 1
            device_id = r.json()["id"]
            print(f"✓ Connected as device #{device_id}")
            
            # Collect and send metrics
            payload = collect_metrics()
            print(json.dumps(payload, indent=2))
            r = requests.post(
                f"{SERVER_URL}/api/device/{device_id}/metrics", 
                headers=HEADERS, 
                json=payload
            )
            if r.status_code == 200:
                print(f"✓ Metrics sent (ID: {r.json()['id']})")
            else:
                print(f"⨯ Failed to send metrics: HTTP {r.status_code}")
                return 1
        else:
            print(f"Starting metrics collection (every {args.interval}s)")
            send_metrics_with_backoff(initial_delay=args.interval)
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1

if __name__ == "__main__":
    main()
