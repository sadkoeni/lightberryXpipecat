python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
cp lightberry.service /etc/systemd/system/
sudo systemctl daemon-reload
sydo systemctl restart lightberry
