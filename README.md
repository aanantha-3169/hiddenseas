# Hidden SEA - Jakarta Audio Tours

**Hidden SEA** is an immersive audio walking tour application for Jakarta, Indonesia. It explores the city's hidden history, from the haunted canals of Old Batavia to the punk rock spirit of Cikini.

## Features

-   **Curated Audio Tours:** High-quality audio narratives for specific routes.
-   **Interactive Maps:** Custom atmospheric maps and Google Maps integration for navigation.
-   **Vibe Check:** Curated Spotify playlists for each tour to set the mood.
-   **Offline Guides:** Downloadable PDF guides for every route.
-   **AI-Enhanced:** (Optional) AI personas for each tour guide.

## Tech Stack

-   **Backend:** Python (Flask)
-   **Frontend:** HTML, Tailwind CSS
-   **Deployment:** Ready for Render (includes `Procfile` and `gunicorn`)

## Local Development

1.  **Clone the repo:**
    ```bash
    git clone https://github.com/aanantha-3169/hiddenseas.git
    cd hiddenseas
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Run the app:**
    ```bash
    python app.py
    ```
    Open `http://127.0.0.1:5001` in your browser.

## Deployment

This app is configured for deployment on **Render**.

1.  Push code to GitHub.
2.  Connect repo to Render.
3.  Add `GOOGLE_API_KEY` to Environment Variables.
4.  Deploy!
