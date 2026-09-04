# MiniBank Demo

Demo only. No real money or bank connections.

Run on Windows:

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set ADMIN_USERNAME=admin
set ADMIN_PASSWORD=ChooseASecretPassword123
set SECRET_KEY=LongRandomSecretKey
flask --app app run --host=0.0.0.0 --port=5000
