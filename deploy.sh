#!/usr/bin/env bash

# Removing any existing virtual environment
echo "Removing existing virtual environment..."
rm -rf venv

# Check if ensurepip is available
if ! python3 -m ensurepip --version &> /dev/null; then
  echo "ensurepip not available, manually installing pip..."
  curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
  python3 get-pip.py --user
  export PATH=$HOME/.local/bin:$PATH
fi

# Creating a new virtual environment using python3
echo "Creating virtual environment using python3..."
python3 -m venv venv
if [ $? -ne 0 ]; then
  echo "Failed to create virtual environment"
  exit 1
fi

# Activating the virtual environment
echo "Activating virtual environment..."
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
  source venv/Scripts/activate
else
  echo "Error: venv activation script not found."
  exit 1
fi

# Check if the virtual environment is activated
if [ -z "$VIRTUAL_ENV" ]; then
  echo "Virtual environment is not activated."
  exit 1
fi

# Upgrade pip and install dependencies
echo "Upgrading pip and installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Install Node.js directly if not available
if ! command -v node &> /dev/null; then
  echo "Node.js not found, installing Node.js"
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.1/install.sh | bash
  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
  nvm install 18
  nvm use 18
fi

# Ensure Node.js version is set correctly
export PATH=$NVM_DIR/versions/node/v18.*/bin:$PATH

# Ensure frontend directory exists and restore npm packages
if [ -d "frontend" ]; then
  echo "Restoring npm packages in frontend directory..."
  cd frontend
  npm install || (echo "Failed to restore frontend npm packages. See above for logs." && exit 1)
  cd ..
else
  echo "Frontend directory not found"
fi

# Ensure the application directory exists and change to it
cd /home/site/wwwroot || exit
