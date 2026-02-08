import os
import google.generativeai as genai
from flask import Flask, render_template, request, jsonify, abort
from dotenv import load_dotenv
from data import tours

# Load environment variables
load_dotenv()

# Configure Gemini
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    # Just a warning, app can still run without AI features
    pass
else:
    genai.configure(api_key=api_key)

app = Flask(__name__)

# Templates
# Using separate template file for tour.html now


# Routes
@app.route('/')
def index():
    return render_template('index.html', tours=tours)

@app.route('/tour/<tour_id>')
def tour_detail(tour_id):
    tour = tours.get(tour_id)
    if not tour:
        abort(404)
    # Pass tour_id explicitly as well
    return render_template('tour.html', tour=tour, tour_id=tour_id)

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    tour_id = data.get('tour_id')

    if not user_message or not tour_id:
        return jsonify({"error": "Missing message or tour_id"}), 400

    tour = tours.get(tour_id)
    if not tour:
        return jsonify({"error": "Invalid tour"}), 404

    try:
        if not api_key:
             return jsonify({"response": "AI feature is not configured (API Key missing). I am just a placeholder response."})

        # Use the tour's specific system prompt
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=tour['system_prompt']
        )
        
        response = model.generate_content(user_message)
        return jsonify({"response": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5001)
