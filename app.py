import os
import re
import json
import uuid
import datetime
import threading
import google.generativeai as genai
import requests as http_requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, abort, session, redirect, url_for
from dotenv import load_dotenv
from supabase import create_client, Client
from data import tours
from functools import wraps

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
_secret_key = os.getenv("FLASK_SECRET_KEY")
if not _secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable must be set")
app.secret_key = _secret_key

# Supabase clients
SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY")
SERVICE_ROLE_KEY = os.getenv("SERVICE_ROLE_KEY")

# Anon client — used only for auth token validation (supabase.auth.get_user)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Admin client — uses service role key, bypasses RLS; used for all server-side DB operations
if not SERVICE_ROLE_KEY:
    raise RuntimeError("SERVICE_ROLE_KEY environment variable must be set")
db: Client = create_client(SUPABASE_URL, SERVICE_ROLE_KEY)

# Badge tiers — computed dynamically from checks_completed, no extra DB table needed
BADGE_TIERS = [
    {"id": "first_step",      "name": "First Step",      "icon": "🌱", "description": "Completed your first Vibe Check",  "threshold": 1},
    {"id": "trail_walker",    "name": "Trail Walker",    "icon": "🚶", "description": "Completed 3 Vibe Checks",          "threshold": 3},
    {"id": "vibe_checker",    "name": "Vibe Checker",    "icon": "✅", "description": "Completed 5 Vibe Checks",          "threshold": 5},
    {"id": "history_hunter",  "name": "History Hunter",  "icon": "🏆", "description": "Completed 10 Vibe Checks",         "threshold": 10},
    {"id": "sea_legend",      "name": "SEA Legend",      "icon": "⭐", "description": "Completed 20 Vibe Checks",         "threshold": 20},
]

def _compute_badges(checks_completed: int) -> list:
    """Return all badge tiers the viber has unlocked."""
    return [b for b in BADGE_TIERS if checks_completed >= b["threshold"]]

# ── Rate Limiter ──────────────────────────────────────────────────────────────
# In-memory, keyed by (user_id, endpoint). Resets naturally as timestamps age out.
# Limits per user per 24-hour rolling window:
RATE_LIMITS = {
    "create_tour":    3,   # Gemini full-tour generation — most expensive
    "chat":          20,   # Gemini chat messages
    "nearby_places": 10,   # Google Places API lookups
    "resolve_maps":   5,   # Gemini map-URL extraction
}
_rate_store: dict = {}          # {(user_id, endpoint): [timestamp, ...]}
_rate_lock  = threading.Lock()

def _check_rate_limit(endpoint: str) -> tuple[bool, int]:
    """Returns (allowed, remaining). Caller must be authenticated."""
    user_id = session.get('user', {}).get('id')
    if not user_id:
        return False, 0

    limit = RATE_LIMITS.get(endpoint, 10)
    now   = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(hours=24)
    key   = (user_id, endpoint)

    with _rate_lock:
        timestamps = _rate_store.get(key, [])
        # Drop calls older than 24 h
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= limit:
            _rate_store[key] = timestamps
            return False, 0
        timestamps.append(now)
        _rate_store[key] = timestamps
        return True, limit - len(timestamps)

# Auth Decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated_function

def viber_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login', next=request.path))
        role = session['user'].get('role')
        if role == 'pending_viber':
            return redirect(url_for('become_viber', pending='1'))
        if role not in ['viber', 'admin']:
            return redirect(url_for('become_viber', from_board='1'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login', next=request.path))
        if session['user'].get('role') != 'admin':
            return abort(403)
        return f(*args, **kwargs)
    return decorated_function

# Templates
# Using separate template file for tour.html now


# Routes
@app.route('/')
def index():
    user = session.get('user')
    return render_template('index.html', tours=tours, user=user)

@app.route('/login')
def login():
    if 'user' in session:
        return redirect(url_for('index'))
    error = request.args.get('error')
    next_url = request.args.get('next', '')
    return render_template('login.html', error=error, next_url=next_url)

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/login/google')
def login_google():
    """Redirect to Supabase Google OAuth. Encodes the post-login destination
    directly into the callback URL so it survives the cross-origin OAuth round-trip."""
    next_url = request.args.get('next', '') or ''
    # Only allow relative paths to prevent open-redirect abuse; default to homepage
    if not next_url or not next_url.startswith('/'):
        next_url = '/'
    import urllib.parse
    app_base_url = os.getenv("APP_BASE_URL", "http://localhost:5001")
    # Pass next_url as a query param on the callback so we don't rely on the
    # session cookie surviving the Supabase cross-origin redirect.
    callback_url = f"{app_base_url}/auth/callback?next={urllib.parse.quote(next_url, safe='')}"
    oauth_url = f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={urllib.parse.quote(callback_url, safe='')}"
    return redirect(oauth_url)

@app.route('/auth/callback')
def auth_callback():
    """Receives the OAuth redirect. Serves a JS bridge that reads
    the access_token from the URL fragment and POSTs it to /auth/session."""
    return render_template('auth_callback.html')


def _unique_handle(base: str) -> str:
    """Return the base handle if unclaimed, otherwise append a suffix until unique."""
    import re as _re
    candidate = _re.sub(r'[^a-z0-9_]', '', base.lower().replace(' ', '_'))[:30] or 'user'
    for attempt in range(6):
        resp = db.table('profiles').select('id').eq('handle', candidate).execute()
        if not resp.data:
            return candidate
        candidate = f"{candidate[:25]}_{uuid.uuid4().hex[:4]}"
    return f"user_{uuid.uuid4().hex[:8]}"

@app.route('/auth/session', methods=['POST'])
def auth_session():
    """Validates the Supabase access token and creates a Flask session."""
    data = request.json
    access_token = data.get('access_token')
    refresh_token = data.get('refresh_token')
    # next_url is now passed directly in the POST body by auth_callback.html
    # (encoded in the callback URL by login_google, survives the OAuth round-trip)
    raw_next = data.get('next_url', '') or ''
    next_url = raw_next if raw_next.startswith('/') else '/'

    if not access_token:
        return jsonify({"error": "No token provided"}), 400

    try:
        user_resp = supabase.auth.get_user(access_token)
        user = user_resp.user

        if not user:
            return jsonify({"error": "Invalid token"}), 401

        meta = user.user_metadata or {}
        user_id = user.id
        display_name = meta.get('full_name') or meta.get('name') or user.email.split('@')[0]

        profile_resp = db.table('profiles').select('*').eq('id', user_id).execute()

        if not profile_resp.data:
            handle = _unique_handle(display_name)
            try:
                db.table('profiles').insert({
                    "id": user_id,
                    "display_name": display_name,
                    "handle": handle,
                    "role": 'user'
                }).execute()
                user_role = 'user'
            except Exception as e:
                print(f"[Auth] Error creating profile: {e}")
                return jsonify({"error": "Account setup failed. Please try again."}), 500
        else:
            profile = profile_resp.data[0]
            user_role = profile.get('role', 'user')
            handle = profile.get('handle', '')

        session['user'] = {
            "id": user_id,
            "email": user.email,
            "name": display_name,
            "picture": meta.get('avatar_url') or meta.get('picture') or '',
            "access_token": access_token,
            "refresh_token": refresh_token,
            "role": user_role,
            "handle": handle
        }
        return jsonify({"status": "ok", "redirect": next_url})
    except Exception as e:
        print(f"[Auth] Error validating token: {e}")
        return jsonify({"error": "Authentication failed. Please try again."}), 401

@app.route('/become-viber', methods=['GET', 'POST'])
@login_required
def become_viber():
    # Already applied — show pending state instead of the form
    from_board = request.args.get('from_board') == '1'

    if session['user'].get('role') == 'pending_viber':
        return render_template('become_viber.html', is_pending=True, from_board=from_board)

    if request.method == 'POST':
        handle = request.form.get('handle')
        bio = request.form.get('bio')
        gopay_number = request.form.get('gopay_number')

        user_id = session['user']['id']
        try:
            db.table('profiles').update({
                "handle": handle,
                "bio": bio,
                "gopay_number": gopay_number,
                "role": "pending_viber"
            }).eq('id', user_id).execute()

            session['user']['role'] = "pending_viber"
            session['user']['handle'] = handle
            session.modified = True

            return render_template('become_viber_success.html')
        except Exception as e:
            return render_template('become_viber.html', error=str(e), is_pending=False, from_board=from_board)

    return render_template('become_viber.html', is_pending=False, from_board=from_board)

@app.route('/vibe')
@viber_required
def bounty_board():
    user_id = session['user']['id']

    # Fresh profile stats
    profile_resp = db.table('profiles').select('checks_completed, bounties_earned').eq('id', user_id).execute()
    profile = profile_resp.data[0] if profile_resp.data else {}
    checks_completed = int(profile.get('checks_completed') or 0)
    bounties_earned  = int(profile.get('bounties_earned')  or 0)
    badges = _compute_badges(checks_completed)

    # All active claims across all vibers (so we know what's taken)
    active_resp = db.table('vibe_checks').select('tour_id, viber_id, status').in_('status', ['claimed', 'submitted']).execute()
    # Map tour_id → claim info
    active_claims = {row['tour_id']: row for row in (active_resp.data or [])}

    bounties = []
    for tour_id, tour in tours.items():
        if tour.get('is_vetted'):
            continue
        t = tour.copy()
        claim = active_claims.get(tour_id)
        if claim and claim['viber_id'] == user_id:
            t['claim_state'] = 'mine'          # this viber has it claimed/submitted
            t['claim_status'] = claim['status']
        elif claim:
            t['claim_state'] = 'taken'         # another viber has it
        else:
            t['claim_state'] = 'available'
        bounties.append({"id": tour_id, "tour": t})

    return render_template('bounty_board.html',
                           bounties=bounties,
                           checks_completed=checks_completed,
                           bounties_earned=bounties_earned,
                           badges=badges)

@app.route('/vibe/claim/<tour_id>', methods=['POST'])
@viber_required
def claim_bounty(tour_id):
    if tour_id not in tours:
        return abort(404)
        
    user_id = session['user']['id']
    
    # Check if already claimed by anyone (with active/pending status)
    check_resp = db.table('vibe_checks').select('*').eq('tour_id', tour_id).in_('status', ['claimed', 'submitted', 'approved']).execute()
    if check_resp.data:
        return jsonify({"error": "Bounty already claimed or tour already vetted"}), 400
        
    # Create claim
    try:
        db.table('vibe_checks').insert({
            "tour_id": tour_id,
            "viber_id": user_id,
            "status": "claimed",
            "bounty_amount": 100000,
            "claim_expires_at": (datetime.datetime.now() + datetime.timedelta(days=7)).isoformat()
        }).execute()
        return redirect(url_for('tour_detail', tour_id=tour_id))
    except Exception as e:
        print(f"[Vibe] Error claiming bounty: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/vibe/submit/<tour_id>', methods=['GET', 'POST'])
@viber_required
def submit_vibe_check(tour_id):
    if tour_id not in tours:
        return abort(404)
        
    user_id = session['user']['id']
    tour = tours[tour_id]
    
    # 1. Verify user has a 'claimed' vibe check for this tour
    check_resp = db.table('vibe_checks').select('*').eq('tour_id', tour_id).eq('viber_id', user_id).eq('status', 'claimed').execute()
    if not check_resp.data:
        return redirect(url_for('tour_detail', tour_id=tour_id))
    
    vibe_check_id = check_resp.data[0]['id']
    
    if request.method == 'POST':
        overall_notes = request.form.get('overall_notes')
        
        # 2. Process each stop
        for idx, stop in enumerate(tour.get('stops', [])):
            # Capture GPS & Ratings
            lat = request.form.get(f'stop_{idx}_lat')
            lng = request.form.get(f'stop_{idx}_lng')
            rating = request.form.get(f'stop_{idx}_rating')
            is_accurate = request.form.get(f'stop_{idx}_accurate') == 'on'
            notes = request.form.get(f'stop_{idx}_notes')
            
            # Handle Media Uploads
            photo_paths = []
            # Photos (3 per stop)
            for p_idx in range(3):
                file = request.files.get(f'stop_{idx}_photo_{p_idx}')
                if file and file.filename != '':
                    # Path: tours/TOUR_ID/VIBE_CHECK_ID/stop_IDX/photo_PIDX.ext
                    ext = file.filename.split('.')[-1]
                    storage_path = f"tours/{tour_id}/{vibe_check_id}/stop_{idx}/photo_{p_idx}.{ext}"
                    try:
                        # Upload to Supabase Storage ('vibe_check_media' bucket)
                        db.storage.from_('vibe_check_media').upload(
                            path=storage_path,
                            file=file.read(),
                            file_options={"content-type": file.content_type}
                        )
                        photo_paths.append(storage_path)
                    except Exception as e:
                        print(f"[Upload Error] {e}")
            
            # Video (1 per stop)
            video_path = None
            video_file = request.files.get(f'stop_{idx}_video')
            if video_file and video_file.filename != '':
                ext = video_file.filename.split('.')[-1]
                v_storage_path = f"tours/{tour_id}/{vibe_check_id}/stop_{idx}/video.{ext}"
                try:
                    db.storage.from_('vibe_check_media').upload(
                        path=v_storage_path,
                        file=video_file.read(),
                        file_options={"content-type": video_file.content_type}
                    )
                    video_path = v_storage_path
                except Exception as e:
                    print(f"[Upload Error Video] {e}")

            # 3. Save stop entry to Supabase
            db.table('vibe_check_stops').insert({
                "vibe_check_id": vibe_check_id,
                "stop_index": idx,
                "stop_name": stop.get('name'),
                "photo_paths": photo_paths,
                "video_path": video_path,
                "gps_lat": float(lat) if lat else None,
                "gps_lng": float(lng) if lng else None,
                "rating": int(rating) if rating else None,
                "is_accurate": is_accurate,
                "notes": notes
            }).execute()

        # 4. Update the vibe_check status to 'submitted'
        db.table('vibe_checks').update({
            "status": "submitted",
            "overall_notes": overall_notes,
            "submitted_at": datetime.datetime.now().isoformat()
        }).eq('id', vibe_check_id).execute()
        
        return render_template('vibe_submission_success.html')

    return render_template('vibe_submission.html', tour=tour, tour_id=tour_id)

@app.route('/tour/<tour_id>')
def tour_detail(tour_id):
    if tour_id not in tours:
        abort(404)
    tour = tours[tour_id].copy() 
    
    # Enrich tour with Vibe Check data from Supabase
    try:
        # Check for approved vibe check
        vibe_resp = db.table('vibe_checks').select('*, profiles(handle, display_name)').eq('tour_id', tour_id).eq('status', 'approved').execute()
        if vibe_resp.data:
            tour['is_vetted'] = True
            vibe_data = vibe_resp.data[0]
            tour['viber_handle'] = vibe_data.get('profiles', {}).get('handle')
            tour['vetted_date'] = vibe_data.get('approved_at', vibe_data.get('submitted_at', ''))[:10]
        else:
            # Check if it's currently claimed by the logged-in user
            if 'user' in session:
                user_id = session['user']['id']
                claim_resp = db.table('vibe_checks').select('status').eq('tour_id', tour_id).eq('viber_id', user_id).eq('status', 'claimed').execute()
                if claim_resp.data:
                    tour['user_has_claim'] = True
                
                # Also check if it's already submitted and pending
                pending_resp = db.table('vibe_checks').select('status').eq('tour_id', tour_id).eq('viber_id', user_id).eq('status', 'submitted').execute()
                if pending_resp.data:
                    tour['vibe_status'] = 'submitted'
    except Exception as e:
        print(f"[Tour] Error fetching vibe check data: {e}")

    return render_template('tour.html', tour=tour, tour_id=tour_id)

# ──────────────────────────────────────────────────────────────────────
# Admin Routes
# ──────────────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_dashboard():
    pending_resp = db.table('vibe_checks').select('*, profiles(handle, display_name)').eq('status', 'submitted').order('submitted_at', desc=True).execute()
    recent_resp = db.table('vibe_checks').select('*, profiles(handle, display_name)').eq('status', 'approved').order('approved_at', desc=True).limit(10).execute()
    pending_vibers_resp = db.table('profiles').select('*').eq('role', 'pending_viber').execute()

    for check in pending_resp.data:
        check['tour_title'] = tours.get(check.get('tour_id'), {}).get('title', check.get('tour_id'))
    for check in recent_resp.data:
        check['tour_title'] = tours.get(check.get('tour_id'), {}).get('title', check.get('tour_id'))

    return render_template('admin_dashboard.html',
                           pending=pending_resp.data,
                           recent=recent_resp.data,
                           pending_vibers=pending_vibers_resp.data)

@app.route('/admin/viber/<user_id>/approve-role', methods=['POST'])
@admin_required
def admin_approve_viber(user_id):
    try:
        db.table('profiles').update({"role": "viber"}).eq('id', user_id).execute()
        return redirect(url_for('admin_dashboard'))
    except Exception as e:
        print(f"[Admin] Error approving viber {user_id}: {e}")
        return jsonify({"error": "Could not approve application. Please try again."}), 500

@app.route('/admin/vibe/<vibe_check_id>')
@admin_required
def admin_vibe_detail(vibe_check_id):
    # 1. Fetch main check data
    check_resp = db.table('vibe_checks').select('*, profiles(*)').eq('id', vibe_check_id).execute()
    if not check_resp.data:
        return abort(404)
    check = check_resp.data[0]
    
    # 2. Fetch all stop evidence
    stops_resp = db.table('vibe_check_stops').select('*').eq('vibe_check_id', vibe_check_id).order('stop_index').execute()
    
    # 3. Handle media URLs (making them public/signed if needed)
    for stop in stops_resp.data:
        # Generate signed/public URLs for each photo
        ps = []
        for path in (stop.get('photo_paths') or []):
            url = db.storage.from_('vibe_check_media').get_public_url(path)
            ps.append(url)
        stop['photo_urls'] = ps
        # Video URL
        if stop.get('video_path'):
            stop['video_url'] = db.storage.from_('vibe_check_media').get_public_url(stop['video_path'])
            
    # Add tour info
    tour_id = check.get('tour_id')
    check['tour_title'] = tours.get(tour_id, {}).get('title', tour_id)
        
    return render_template('admin_vibe_detail.html', check=check, stops=stops_resp.data)

@app.route('/viber/<handle>')
def viber_profile(handle):
    """Public profile page for an approved Viber."""
    profile_resp = db.table('profiles').select('*').eq('handle', handle).execute()
    if not profile_resp.data:
        abort(404)
    profile = profile_resp.data[0]

    if profile.get('role') not in ['viber', 'admin']:
        abort(404)

    checks_completed = int(profile.get('checks_completed') or 0)
    bounties_earned  = int(profile.get('bounties_earned')  or 0)
    badges = _compute_badges(checks_completed)

    # Fetch approved vibe checks for this viber
    checks_resp = db.table('vibe_checks').select('*').eq('viber_id', profile['id']).eq('status', 'approved').order('approved_at', desc=True).execute()
    for check in checks_resp.data:
        check['tour_title'] = tours.get(check.get('tour_id'), {}).get('title', check.get('tour_id', ''))
        check['tour_location'] = tours.get(check.get('tour_id'), {}).get('location', '')

    return render_template('viber_profile.html',
                           profile=profile,
                           completed_checks=checks_resp.data,
                           checks_completed=checks_completed,
                           bounties_earned=bounties_earned,
                           badges=badges,
                           user=session.get('user'))

@app.route('/admin/vibe/<vibe_check_id>/approve', methods=['POST'])
@admin_required
def admin_approve_vibe(vibe_check_id):
    try:
        # 1. Fetch the check to get viber_id and bounty_amount
        check_resp = db.table('vibe_checks').select('viber_id, bounty_amount').eq('id', vibe_check_id).execute()
        if not check_resp.data:
            return abort(404)
        check = check_resp.data[0]
        viber_id = check['viber_id']
        bounty_amount = check.get('bounty_amount') or 100000

        # 2. Increment checks_completed and bounties_earned on the viber's profile
        profile_resp = db.table('profiles').select('checks_completed, bounties_earned').eq('id', viber_id).execute()
        if profile_resp.data:
            p = profile_resp.data[0]
            db.table('profiles').update({
                'checks_completed': (p.get('checks_completed') or 0) + 1,
                'bounties_earned':  (p.get('bounties_earned')  or 0) + bounty_amount,
                'updated_at': datetime.datetime.now().isoformat()
            }).eq('id', viber_id).execute()

        # 3. Mark the vibe check as approved
        db.table('vibe_checks').update({
            "status": "approved",
            "approved_at": datetime.datetime.now().isoformat()
        }).eq('id', vibe_check_id).execute()

        return redirect(url_for('admin_dashboard'))
    except Exception as e:
        print(f"[Admin] Error approving vibe check: {e}")
        return jsonify({"error": "Could not approve. Please try again."}), 500

@app.route('/api/chat', methods=['POST'])
def chat():
    allowed, remaining = _check_rate_limit("chat")
    if not allowed:
        return jsonify({"error": "You've reached the daily chat limit (20 messages). Come back tomorrow!"}), 429

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
            model_name="gemini-3-flash",
            system_instruction=tour['system_prompt']
        )
        
        response = model.generate_content(user_message)
        return jsonify({"response": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Directory for persisted generated tours
GENERATED_DIR = os.path.join(os.path.dirname(__file__), 'static', 'generated')
os.makedirs(GENERATED_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────
# Tour Generation Prompt
# ──────────────────────────────────────────────────────────────────────
TOUR_GENERATION_PROMPT = """You are a world-class walking tour writer for Hidden SEA, \
an app that creates immersive, opinionated audio walking tours of Southeast Asian cities.

Your tours are NOT sanitized tourist content. They are gritty, historically layered, \
and full of dramatic narrative tension. You write like a documentary filmmaker — \
punchy sentences, dramatic contrasts, specific numbers and dates, and \
visceral sensory details.

## YOUR TASK
Create an immersive walking tour based on:
- **Location:** {location}
- **Themes/Tags:** {tags}

## OUTPUT FORMAT (follow this EXACTLY)

### SECTION 1: NARRATIVE (plain text, for NotebookLM podcast generation)

Write the full tour narrative in this exact structure:

**Introduction**
[2-3 sentences that set the theme with a dramatic hook. Preview what the walker will see. \
Establish the narrative arc/contradiction of the tour.]

**Stop 1: [Place Name] ([Evocative Subtitle])**
[Walking directions from previous stop if not the first.]
[Labeled observations — use headers like "The Architecture:", "The Context:", "The Vibe:", \
"The Symbolism:", "The History:" etc. Each should be 2-4 punchy sentences.]
[At least one specific ACTION for the walker: "Look up at...", "Turn around...", \
"Find the...", "Order the..."]

[Repeat for 3-5 stops total]

**Additional Facts**
[2-3 deeper dives on food spots, social norms, or vantage points. Each has:]
- Location: [specific walking distance from a stop]
- The Narrative: [2-3 quotable sentences in the tour voice]
- The Action: [specific thing to do, with prices in IDR if applicable]
- The tone should be immersive, opinionated, and highly specific to the selected theme.

### SECTION 2: STRUCTURED DATA (valid JSON block)

After the narrative, output a JSON block fenced in ```json``` with this schema:
```json
{{
  "title": "Tour Title (dramatic, max 8 words)",
  "description": "One-line hook (max 20 words)",
  "vibe": "from-[color]-800 to-[color]-950 (use forest, emerald, lichen, or moss greens)",
  "system_prompt": "You... [persona, 2 sentences]",
  "stops": [
    {{
      "name": "Place Name",
      "subtitle": "Evocative Subtitle",
      "lat": -6.XXXXX,
      "lng": 106.XXXXX,
      "description": "One-line summary"
    }}
  ]
}}
```

## RULES
- Always use REAL places with ACCURATE coordinates (lat/lng to 5+ decimal places)
- Each stop must be WALKABLE from the previous (within ~15 min walk)
- The tour should take 60-90 minutes total on foot
- Write for an English-speaking expat audience — assume cultural curiosity but no local language fluency
- Be opinionated and dramatic. No generic tourist copy.
- Include at least one food/drink recommendation with a specific order
- End the tour at a reflective vantage point (sunset, park, river, rooftop)
"""


@app.route('/create-tour')
@login_required
def create_tour():
    return render_template('create_tour.html')

@app.route('/api/create-tour', methods=['POST'])
@login_required
def api_create_tour():
    """Generate a full tour narrative using Gemini."""
    allowed, remaining = _check_rate_limit("create_tour")
    if not allowed:
        return jsonify({"error": "You've reached the daily tour generation limit (3 per day). Come back tomorrow!"}), 429

    data = request.json
    location = data.get('location', '').strip()
    tags = data.get('tags', [])

    if not location:
        return jsonify({"error": "Location is required"}), 400
    if not tags or len(tags) < 1:
        return jsonify({"error": "At least one tag is required"}), 400

    if not api_key:
        return jsonify({"error": "AI not configured (API key missing)"}), 500

    try:
        # Format the prompt
        tags_str = ", ".join(tags[:3])
        prompt = TOUR_GENERATION_PROMPT.format(location=location, tags=tags_str)

        # Call Gemini
        model = genai.GenerativeModel(model_name="gemini-3-flash-preview")
        response = model.generate_content(prompt)
        raw_text = response.text

        # Parse: split narrative from JSON
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
        if not json_match:
            # Try without code fences
            json_match = re.search(r'(\{[\s\S]*"stops"[\s\S]*\})', raw_text)

        if not json_match:
            return jsonify({
                "error": "AI generated content but we couldn't parse the structured data. Please try again.",
                "raw_narrative": raw_text
            }), 500

        structured_data = json.loads(json_match.group(1))

        # Extract the narrative (everything before the JSON block)
        json_start = raw_text.find('```json')
        if json_start == -1:
            json_start = raw_text.find(json_match.group(0))
        narrative = raw_text[:json_start].strip()

        # Clean up markdown formatting from narrative
        narrative = re.sub(r'^#+\s*SECTION\s*1.*$', '', narrative, flags=re.MULTILINE)
        narrative = re.sub(r'^#+\s*NARRATIVE.*$', '', narrative, flags=re.MULTILINE)
        narrative = narrative.strip()

        # Generate tour ID
        tour_id = str(uuid.uuid4())[:8]

        # Build tour object
        tour_data = {
            "id": tour_id,
            "title": structured_data.get("title", f"Tour of {location}"),
            "description": structured_data.get("description", "An AI-generated walking tour"),
            "vibe": structured_data.get("vibe", "from-[#2D5A27] to-emerald-950"),
            "system_prompt": structured_data.get("system_prompt", f"You are a local guide for {location}."),
            "stops": structured_data.get("stops", []),
            "narrative": narrative,
            "location": location,
            "tags": tags,
            "audio_url": None,  # Will be filled by NotebookLM later
            "pdf_link": None,   # Will be filled by NotebookLM later
            "google_maps_url": None,
            "map_url": None,
            "is_vetted": False,
            "rating": 0.0,
            "generated": True,
            "created_at": __import__('datetime').datetime.utcnow().isoformat()
        }

        # Persist to JSON file
        filepath = os.path.join(GENERATED_DIR, f"{tour_id}.json")
        with open(filepath, 'w') as f:
            json.dump(tour_data, f, indent=2)

        print(f"[Create Tour] Generated '{tour_data['title']}' with {len(tour_data['stops'])} stops → {filepath}")

        return jsonify({
            "status": "success",
            "tour_id": tour_id,
            "title": tour_data["title"],
            "stops": tour_data["stops"],
            "narrative_preview": narrative[:300] + "..." if len(narrative) > 300 else narrative
        })

    except json.JSONDecodeError as e:
        print(f"[Create Tour] JSON parse error: {e}")
        return jsonify({"error": "AI output was malformed. Please try again."}), 500
    except Exception as e:
        print(f"[Create Tour] Error: {e}")
        return jsonify({"error": f"Generation failed: {str(e)}"}), 500


@app.route('/tour/generated/<tour_id>')
def generated_tour_detail(tour_id):
    """Serve a generated tour using the same tour template."""
    filepath = os.path.join(GENERATED_DIR, f"{tour_id}.json")
    if not os.path.exists(filepath):
        abort(404)

    with open(filepath, 'r') as f:
        tour = json.load(f)

    # Check if this tour has already been submitted for review
    sub_resp = db.table('submitted_tours').select('status').eq('temp_tour_id', tour_id).execute()
    tour['submission_status'] = sub_resp.data[0]['status'] if sub_resp.data else None

    return render_template('tour.html', tour=tour, tour_id=tour_id)

@app.route('/api/submit-tour', methods=['POST'])
@login_required
def submit_tour():
    """Save a generated tour to Supabase for admin review."""
    data = request.json
    tour_id = data.get('tour_id', '').strip()
    if not tour_id:
        return jsonify({"error": "tour_id required"}), 400

    filepath = os.path.join(GENERATED_DIR, f"{tour_id}.json")
    if not os.path.exists(filepath):
        return jsonify({"error": "Tour not found"}), 404

    # Prevent duplicate submissions
    existing = db.table('submitted_tours').select('id').eq('temp_tour_id', tour_id).execute()
    if existing.data:
        return jsonify({"error": "already_submitted"}), 409

    with open(filepath, 'r') as f:
        tour_data = json.load(f)

    user = session.get('user')
    try:
        db.table('submitted_tours').insert({
            "temp_tour_id":    tour_id,
            "title":           tour_data.get('title'),
            "description":     tour_data.get('description'),
            "location":        tour_data.get('location'),
            "tags":            tour_data.get('tags', []),
            "stops":           tour_data.get('stops', []),
            "narrative":       tour_data.get('narrative'),
            "system_prompt":   tour_data.get('system_prompt'),
            "submitter_id":    user['id']    if user else None,
            "submitter_name":  user['name']  if user else None,
            "submitter_email": user['email'] if user else None,
            "status":          "pending"
        }).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"[Submit Tour] Error: {e}")
        return jsonify({"error": "Submission failed. Please try again."}), 500

@app.route('/admin/submissions')
@admin_required
def admin_submissions():
    """List all submitted tour ideas for review."""
    resp = db.table('submitted_tours').select('*').order('submitted_at', desc=True).execute()
    return render_template('admin_submissions.html', submissions=resp.data)

@app.route('/admin/submission/<submission_id>')
@admin_required
def admin_submission_detail(submission_id):
    """View a single submitted tour idea."""
    resp = db.table('submitted_tours').select('*').eq('id', submission_id).execute()
    if not resp.data:
        abort(404)
    return render_template('admin_submission_detail.html', sub=resp.data[0])

@app.route('/admin/submission/<submission_id>/approve', methods=['POST'])
@admin_required
def admin_approve_submission(submission_id):
    db.table('submitted_tours').update({
        "status": "approved",
        "reviewed_at": datetime.datetime.now().isoformat()
    }).eq('id', submission_id).execute()
    return redirect(url_for('admin_submissions'))

@app.route('/admin/submission/<submission_id>/reject', methods=['POST'])
@admin_required
def admin_reject_submission(submission_id):
    admin_notes = request.form.get('notes', '')
    db.table('submitted_tours').update({
        "status": "rejected",
        "admin_notes": admin_notes,
        "reviewed_at": datetime.datetime.now().isoformat()
    }).eq('id', submission_id).execute()
    return redirect(url_for('admin_submissions'))

@app.route('/api/resolve-maps', methods=['POST'])
def resolve_maps():
    """Resolve a Google Maps list URL and extract place data using Gemini."""
    allowed, remaining = _check_rate_limit("resolve_maps")
    if not allowed:
        return jsonify({"error": "You've reached the daily map resolution limit (5 per day). Come back tomorrow!"}), 429

    data = request.json
    url = data.get('url', '').strip()

    if not url:
        return jsonify({"error": "Missing URL"}), 400

    default_center = {"lat": -6.2088, "lng": 106.8456}

    try:
        # Step 1: Resolve short URL
        # Use curl-like User-Agent to get HTTP 302 redirect (browser UA gets a JS-only page)
        resolved_url = url
        if 'goo.gl' in url or 'maps.app' in url:
            try:
                resp = http_requests.head(url, headers={'User-Agent': 'curl/8.0'}, timeout=10, allow_redirects=False)
                if resp.status_code in (301, 302) and 'Location' in resp.headers:
                    resolved_url = resp.headers['Location']
            except Exception:
                pass

        # Step 2: Extract center coordinates from the resolved URL
        center = None
        at_match = re.search(r'@(-?\d+\.?\d*),(-?\d+\.?\d*)', resolved_url)
        if at_match:
            center = {"lat": float(at_match.group(1)), "lng": float(at_match.group(2))}

        d3_match = re.search(r'!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)', resolved_url)
        if d3_match and not center:
            center = {"lat": float(d3_match.group(1)), "lng": float(d3_match.group(2))}

        # Step 3: Fetch the Maps page HTML for Gemini to analyze
        page_html = ""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            page_resp = http_requests.get(resolved_url, headers=headers, timeout=10, allow_redirects=True)
            page_html = page_resp.text[:80000]  # Limit to 80k chars for Gemini
        except Exception:
            pass

        # Step 4: Use Gemini to extract place names and coordinates
        places = []
        if api_key and page_html:
            try:
                model = genai.GenerativeModel(model_name="gemini-3-flash-preview")
                prompt = f"""Analyze this Google Maps page HTML and the URL to extract the list of places/points of interest.

URL: {resolved_url}

HTML (truncated):
{page_html[:60000]}

Extract ALL the places mentioned in this Google Maps list/collection. For each place, provide:
1. The place name
2. The latitude (approximate is fine)  
3. The longitude (approximate is fine)

If you cannot find exact coordinates in the HTML, use your knowledge to provide accurate coordinates for the places based on their names and the city context.

Return ONLY a valid JSON array, no markdown, no explanation. Example format:
[{{"name": "Place Name", "lat": -6.1234, "lng": 106.5678}}]

If you cannot identify any places, return an empty array: []"""

                response = model.generate_content(prompt)
                response_text = response.text.strip()
                
                # Clean up response - remove markdown code blocks if present
                response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
                response_text = re.sub(r'\s*```$', '', response_text)
                
                places = json.loads(response_text)
                
                # Validate the places
                places = [
                    p for p in places 
                    if isinstance(p, dict) 
                    and 'lat' in p and 'lng' in p 
                    and isinstance(p.get('lat'), (int, float))
                    and isinstance(p.get('lng'), (int, float))
                    and -90 <= p['lat'] <= 90 
                    and -180 <= p['lng'] <= 180
                ][:30]
                
                print(f"[Resolve Maps] Gemini extracted {len(places)} places")
                
            except Exception as e:
                print(f"[Resolve Maps] Gemini extraction failed: {e}")

        # Step 5: Compute center from places if needed
        if places and not center:
            avg_lat = sum(p['lat'] for p in places) / len(places)
            avg_lng = sum(p['lng'] for p in places) / len(places)
            center = {"lat": avg_lat, "lng": avg_lng}

        if not center:
            center = default_center

        return jsonify({
            "resolved_url": resolved_url,
            "center": center,
            "places": places,
            "place_count": len(places)
        })

    except Exception as e:
        print(f"[Resolve Maps Error] {e}")
        return jsonify({
            "error": str(e),
            "center": default_center,
            "places": [],
            "place_count": 0
        }), 200

# ──────────────────────────────────────────────────────────────────────
# Google Places API (New) - Nearby Search
# ──────────────────────────────────────────────────────────────────────
PLACES_API_KEY = os.getenv("PLACES_API_KEY")

@app.route('/api/nearby-places', methods=['POST'])
def nearby_places():
    """Find nearby restaurants/cafes and tourist sights using Google Places API (New)."""
    allowed, remaining = _check_rate_limit("nearby_places")
    if not allowed:
        return jsonify({"error": "You've reached the daily nearby places limit (10 per day). Come back tomorrow!"}), 429

    if not PLACES_API_KEY:
        return jsonify({"error": "Places API key not configured"}), 500

    data = request.json
    lat = data.get('lat')
    lng = data.get('lng')
    place_type = data.get('type', 'restaurant')  # 'restaurant' or 'tourist_attraction'
    radius = data.get('radius', 800)  # meters

    if lat is None or lng is None:
        return jsonify({"error": "Missing lat/lng"}), 400

    try:
        url = "https://places.googleapis.com/v1/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": PLACES_API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.photos,places.types,places.googleMapsUri,places.location"
        }

        # Map simplified types to Google Places includedTypes
        type_map = {
            'restaurant': ['restaurant', 'cafe', 'bakery', 'bar'],
            'tourist_attraction': ['tourist_attraction', 'museum', 'historical_landmark', 'cultural_landmark', 'church', 'mosque', 'hindu_temple']
        }

        body = {
            "includedTypes": type_map.get(place_type, [place_type]),
            "maxResultCount": 8,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius
                }
            },
            "rankPreference": "POPULARITY"
        }

        resp = http_requests.post(url, headers=headers, json=body, timeout=10)
        resp_data = resp.json()

        if resp.status_code != 200:
            print(f"[Places API] Error: {resp.status_code} - {resp_data}")
            return jsonify({"error": "Places API error", "details": resp_data}), resp.status_code

        # Transform response into simpler format
        places = []
        for p in resp_data.get('places', []):
            place = {
                "name": p.get('displayName', {}).get('text', 'Unknown'),
                "address": p.get('formattedAddress', ''),
                "rating": p.get('rating', 0),
                "review_count": p.get('userRatingCount', 0),
                "types": p.get('types', []),
                "maps_url": p.get('googleMapsUri', ''),
                "lat": p.get('location', {}).get('latitude'),
                "lng": p.get('location', {}).get('longitude'),
            }
            # Build photo URL if available
            photos = p.get('photos', [])
            if photos:
                photo_name = photos[0].get('name', '')
                if photo_name:
                    place['photo_url'] = f"https://places.googleapis.com/v1/{photo_name}/media?maxHeightPx=300&maxWidthPx=400&key={PLACES_API_KEY}"
            places.append(place)

        return jsonify({"places": places, "count": len(places)})

    except Exception as e:
        print(f"[Places API] Error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001)
