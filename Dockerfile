FROM mcr.microsoft.com/vscode/devcontainers/python:3.11

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    graphviz \
    graphviz-dev \
    git \
    curl \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /workspace

# Copy source code in early
COPY . .

# Upgrade pip and install Python packages as root
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

USER vscode

# Ensure ~/.local/bin is in PATH for the vscode user
RUN echo 'export PATH="$PATH:$HOME/.local/bin"' >> ~/.bashrc
