# Imagen completa del host: Function App Linux Premium o Dedicated (Always On).
FROM mcr.microsoft.com/azure-functions/python:4-python3.12
ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    FUNCTIONS_WORKER_RUNTIME=python \
    FUNCTIONS_WORKER_PROCESS_COUNT=1 \
    PYTHON_THREADPOOL_THREAD_COUNT=4
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt
COPY host.json function_app.py /home/site/wwwroot/
COPY recording_studio /home/site/wwwroot/recording_studio
RUN python -m compileall -q /home/site/wwwroot && ffmpeg -version && ffprobe -version
