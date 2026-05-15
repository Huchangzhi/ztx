import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app
app.run(host='0.0.0.0', port=5000, debug=False)
