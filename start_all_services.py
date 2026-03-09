#!/usr/bin/env python3
# Script to start all RAG system microservices
import os
import sys
import subprocess
import signal
import time
from pathlib import Path

def start_services():
    print("Starting RAG System Microservices...")

    # Define the services to start
    services = [
        "chunker",
        "converter",
        "embedder",
        "indexer",
        "manager",
        "searcher",
        "websearch",
        "webconfig",  # Configuration manager
        "web_ui"  # Web UI service
    ]

    # Get the project root directory
    project_root = Path(__file__).parent
    services_dir = project_root / "services"
    
    # Store running processes
    processes = {}

    # Start each service
    for service in services:
        service_dir = services_dir / service

        # Special case for web_ui which uses web_ui_service.py instead of web_ui.py
        if service == "web_ui":
            main_file = service_dir / "web_ui_service.py"
        else:
            main_file = service_dir / f"{service}.py"

        if main_file.exists():
            print(f"Starting {service} service...")

            try:
                # Start the service in a separate process
                process = subprocess.Popen([sys.executable, str(main_file)], cwd=str(service_dir))
                processes[service] = process
                print(f"{service} service started with PID {process.pid}")

                # Wait a bit between starting services
                time.sleep(2)

            except Exception as e:
                print(f"Error starting {service} service: {e}")
        else:
            print(f"WARNING: {service} service not found at {main_file}")

    print("\nAll services have been started.")
    print("Press Ctrl+C to stop all services.")

    try:
        # Keep the script running
        while True:
            time.sleep(1)
            
            # Check if any processes have terminated
            for service, process in list(processes.items()):
                if process.poll() is not None:
                    print(f"{service} service (PID {process.pid}) has terminated with code {process.returncode}")
                    del processes[service]
                    
                    # Optionally restart the service
                    service_dir = services_dir / service
                    # Special case for web_ui which uses web_ui_service.py instead of web_ui.py
                    if service == "web_ui":
                        main_file = service_dir / "web_ui_service.py"
                    else:
                        main_file = service_dir / f"{service}.py"
                    if main_file.exists():
                        print(f"Restarting {service} service...")
                        new_process = subprocess.Popen([sys.executable, str(main_file)], cwd=str(service_dir))
                        processes[service] = new_process
                        print(f"{service} service restarted with PID {new_process.pid}")
                        
    except KeyboardInterrupt:
        print("\nShutting down all services...")
        for service, process in processes.items():
            try:
                print(f"Stopping {service} service (PID {process.pid})...")
                process.terminate()
                try:
                    process.wait(timeout=5)  # Wait up to 5 seconds for graceful shutdown
                except subprocess.TimeoutExpired:
                    print(f"{service} service didn't stop gracefully, killing it...")
                    process.kill()  # Force kill if it doesn't stop gracefully
            except Exception as e:
                print(f"Error stopping {service} service: {e}")

        print("All services have been stopped.")

if __name__ == "__main__":
    start_services()