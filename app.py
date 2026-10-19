from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        if current_user_id:
            query = f"""
                SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                       (CASE WHEN (
                           EXISTS (SELECT 1 FROM reactions r WHERE r.post_id = p.id AND r.user_id = ?)
                           OR
                           EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = ?)
                       ) THEN 1 ELSE 0 END) as has_engaged
                FROM posts p
                JOIN users u ON p.user_id = u.id
                {where_clause}
                ORDER BY has_engaged ASC, p.created_at DESC
                LIMIT ? OFFSET ?
            """
            final_params = [current_user_id, current_user_id] + params + list(pagination_params)
        else:
            query = f"""
                SELECT p.id, p.content, p.created_at, u.username, u.id as user_id, 0 as has_engaged
                FROM posts p
                JOIN users u ON p.user_id = u.id
                {where_clause}
                ORDER BY p.created_at DESC
                LIMIT ? OFFSET ?
            """
            final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        
    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        
        post_dict = dict(post)

        # --- Calculate risk score for admin feed view ---
        # --- Rule 2.1 & Newcomer Check ---
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        post_dict['is_newcomer'] = False # Default to False
        
        author_info = query_db('SELECT created_at FROM users WHERE id = ?', (post['user_id'],), one=True)
        if author_info and author_info['created_at']:
            dt = author_info['created_at']
            if isinstance(dt, str):
                dt = dt.replace(' ', 'T') # <--- Date fix so the multiplier actually works
                try: dt = datetime.fromisoformat(dt)
                except ValueError: pass
                    
            if isinstance(dt, datetime):
                # Step 3: Apply age-based multiplier for posts (< 7 days)
                if (datetime.utcnow() - dt).days < 7:
                    final_score *= 1.5
                    post_dict['is_newcomer'] = True # <--- Newcomer flag added here!
        
        if final_score >= 5.0:
            post_dict['risk_label'] = 'HIGH'
        elif final_score >= 3.0:
            post_dict['risk_label'] = 'MEDIUM'
        elif final_score >= 1.0:
            post_dict['risk_label'] = 'LOW'
        else:
            post_dict['risk_label'] = 'NONE'
        
        post_dict['risk_score'] = round(min(5.0, final_score), 2)
        # -----------------------------------------------

        post_dict['content'], _ = moderate_content(post_dict['content'])
        
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
            
        # Calculate total reactions and comments across the platform
        total_reactions_count = sum(r['count'] for r in reactions)
        total_comments_count = len(comments_raw)

        # DESIGN CLAIM 1: True if the post has zero reactions and zero comments from anyone
        needs_engagement = True if (total_reactions_count == 0 and total_comments_count == 0) else False

        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated,
            'needs_engagement': needs_engagement
        })

    # Determine if current user is admin
    current_username = session.get('username', '')
    is_admin = (current_username.lower() == 'admin' or session.get('user_id') == 1)

    # 4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, 
                           per_page=POSTS_PER_PAGE, 
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           is_admin=is_admin)

# Calculate risk score for admin feed view
    _, base_score = moderate_content(post_dict['content'])
    final_score = base_score 
    author_info = query_db('SELECT created_at FROM users WHERE id = ?', (post['user_id'],), one=True)
    if author_info and author_info['created_at']:
        dt = author_info['created_at']
        if isinstance(dt, str):
            try: 
                dt = datetime.fromisoformat(dt)
            except ValueError: 
                pass
        if isinstance(dt, datetime):
            if (datetime.utcnow() - dt).days < 7:
                final_score *= 1.5
        
        if final_score >= 5.0:
            post_dict['risk_label'] = 'HIGH'
        elif final_score >= 3.0:
            post_dict['risk_label'] = 'MEDIUM'
        elif final_score >= 1.0:
            post_dict['risk_label'] = 'LOW'
        else:
            post_dict['risk_label'] = 'NONE'
        
        post_dict['risk_score'] = round(min(5.0, final_score), 2)

        post_dict['content'], _ = moderate_content(post_dict['content'])
        
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
            
        # DESIGN CLAIM 1: We check if the user has engaged. If has_engaged is 0, 
        # needs_engagement becomes True, which triggers the orange badge in HTML.
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated,
            'needs_engagement': True if current_user_id and post['has_engaged'] == 0 else False
        })

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/report/post/<int:post_id>', methods=['POST'])
def report_post(post_id):
    """Allows users to report a post to moderators."""
    if not session.get('user_id'):
        flash('You must be logged in to report posts.', 'danger')
        return redirect(url_for('login'))
    
    flash('Thank you. This post has been reported to the moderators for review.', 'success')
    return redirect(url_for('feed'))

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # NEW CODE: Actually pass the content through the moderation function
    moderated_content, score = moderate_content(content)

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
           (user_id, content))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True

    # --- DESIGN CLAIM 13: Activity Dashboard ---
    # Count how many posts this specific user has made
    user_post_count = len(posts)
    
    # Calculate platform average posts per user
    avg_query = query_db('SELECT (SELECT COUNT(*) FROM posts) * 1.0 / (SELECT COUNT(*) FROM users) as avg_posts', one=True)
    platform_avg = round(avg_query['avg_posts'] or 0, 1) if avg_query else 0.0
    
    # Set a challenging goal: either beat the platform average, or if already beating it, hit the next milestone (multiple of 5)
    if user_post_count < platform_avg:
        target_goal = int(platform_avg) + 1
    else:
        target_goal = ((user_post_count // 5) + 1) * 5
    # ----------------------------------------------------

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           # Pass new variables to the template
                           user_post_count=user_post_count,
                           platform_avg=platform_avg,
                           target_goal=target_goal)

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk, with optional risk filtering."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination, current tab, and risk filter parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users')
    risk_filter = request.args.get('risk_filter', 'ALL').upper()

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # --- Users Tab Data ---
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        
        # Apply risk filter if specified
        if risk_filter != 'ALL' and risk_label != risk_filter:
            continue
            
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    # Fetch all posts to filter accurately by risk score since risk is computed in Python
    posts_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC
    ''')
    
    all_posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        
        # Apply risk filter if specified
        if risk_filter != 'ALL' and risk_label != risk_filter:
            continue
            
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        all_posts.append(post_dict)

    all_posts.sort(key=lambda x: x['risk_score'], reverse=True)
    total_posts_count = len(all_posts)
    posts = all_posts[posts_offset : posts_offset + PAGE_SIZE]
    total_posts_pages = max(1, (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE)

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    comments_raw = query_db('''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC
    ''')
    
    all_comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        
        # Apply risk filter if specified
        if risk_filter != 'ALL' and risk_label != risk_filter:
            continue
            
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        all_comments.append(comment_dict)

    all_comments.sort(key=lambda x: x['risk_score'], reverse=True)
    total_comments_count = len(all_comments)
    comments = all_comments[comments_offset : comments_offset + PAGE_SIZE]
    total_comments_pages = max(1, (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           risk_filter=risk_filter,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """


    """
    Calculates the User Risk Score based on Rule 2.2 of the specification.
    """
    user = query_db('SELECT intro, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    # Step 1: Profile Score
    intro = user['intro'] if user['intro'] else ""
    _, profile_score = moderate_content(intro)

    # Step 2: Average Post Score
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    post_total_score = 0.0
    for p in posts:
        _, score = moderate_content(p['content'])
        post_total_score += score
    average_post_score = (post_total_score / len(posts)) if posts else 0.0

    # Step 3: Average Comment Score
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    comment_total_score = 0.0
    for c in comments:
        _, score = moderate_content(c['content'])
        comment_total_score += score
    average_comment_score = (comment_total_score / len(comments)) if comments else 0.0

    # Step 4: Combine Scores
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: Apply Age Multiplier
    user_risk_score = content_risk_score
    if user['created_at']:
        dt = user['created_at']
        if isinstance(dt, str):
            try: dt = datetime.fromisoformat(dt)
            except ValueError: pass
        if isinstance(dt, datetime):
            age_days = (datetime.utcnow() - dt).days
            if age_days < 7:
                user_risk_score *= 1.5
            elif age_days < 30:
                user_risk_score *= 1.2

    # Step 6: Final Capping (Max 5.0)
    return round(min(5.0, user_risk_score), 2)
    
# Assignment 2.1
def moderate_content(content):
    """Filters inappropriate words and calculates a severity score for a single piece of text."""
    if not content:
        return content, 0.0
        
    # --- TEMPORARY TEST RULE ---
    if "testbad" in content.lower():
        return "[content removed due to severe violation]", 5.0
    # ---------------------------
    
    # Stage 1.1: Severe Violation Checks
    for word in TIER1_WORDS:
        if re.search(rf'\b{re.escape(word)}\b', content, re.IGNORECASE):
            return "[content removed due to severe violation]", 5.0
            
    for phrase in TIER2_PHRASES:
        if re.search(rf'\b{re.escape(phrase)}\b', content, re.IGNORECASE):
            return "[content removed due to spam/scam policy]", 5.0
            
    # Stage 1.2: Scored Violations & Filtering
    score = 0.0
    moderated_content = content
    
    for word in TIER3_WORDS:
        pattern = re.compile(rf'\b{re.escape(word)}\b', re.IGNORECASE)
        matches = pattern.findall(moderated_content)
        if matches:
            score += (2.0 * len(matches))
            moderated_content = pattern.sub('*' * len(word), moderated_content)
            
    url_pattern = re.compile(r'https?://[^\s]+|www\.[^\s]+')
    url_matches = url_pattern.findall(moderated_content)
    if url_matches:
        score += (2.0 * len(url_matches))
        moderated_content = url_pattern.sub('[link removed]', moderated_content)
        
    alpha_chars = [c for c in content if c.isalpha()]
    if len(alpha_chars) > 15:
        upper_chars = [c for c in alpha_chars if c.isupper()]
        if (len(upper_chars) / len(alpha_chars)) > 0.70:
            score += 0.5
            
    return moderated_content, score

# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    recommended_posts = {} 

    return recommended_posts;

# --- NEW CODE: Feature 3 (Design Claim 32 - Group Commitment) ---

# 1. Initialize the new database tables safely
with app.app_context():
    db = get_db()
    db.execute('CREATE TABLE IF NOT EXISTS user_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, keyword TEXT, creator_id INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS group_members (group_id INTEGER, user_id INTEGER, PRIMARY KEY (group_id, user_id))')
    db.commit()

# 2. Route to list all groups and create new ones
@app.route('/groups', methods=['GET', 'POST'])
def groups():
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        name = request.form['name']
        keyword = request.form['keyword']
        # Create the group
        query_db('INSERT INTO user_groups (name, keyword, creator_id) VALUES (?, ?, ?)', 
                 (name, keyword, session['user_id']), commit=True)
        # Add the creator to the group automatically
        new_group = query_db('SELECT id FROM user_groups ORDER BY id DESC LIMIT 1', one=True)
        query_db('INSERT INTO group_members (group_id, user_id) VALUES (?, ?)', 
                 (new_group['id'], session['user_id']), commit=True)
        return redirect(url_for('groups'))
        
    all_groups = query_db('SELECT * FROM user_groups')
    
    # NEW CODE: Fetch the groups the current user is a member of
    my_groups = query_db('''
        SELECT g.* FROM user_groups g 
        JOIN group_members gm ON g.id = gm.group_id 
        WHERE gm.user_id = ?
    ''', (session['user_id'],))
    
    return render_template('groups.html.j2', groups=all_groups, my_groups=my_groups)

# 3. Route to view a specific group and see member suggestions
@app.route('/groups/<int:group_id>')
def group_detail(group_id):
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    group = query_db('SELECT * FROM user_groups WHERE id = ?', (group_id,), one=True)
    if not group:
        abort(404)
        
    members = query_db('''
        SELECT u.username FROM users u 
        JOIN group_members gm ON u.id = gm.user_id 
        WHERE gm.group_id = ?
    ''', (group_id,))
    
    # --- DESIGN CLAIM 32: Group Commitment ---
    # Find users whose past posts contain the group's keyword.
    # If they talked about it, they might want to join the group
    keyword_search = f"%{group['keyword']}%"
    suggestions = query_db('''
        SELECT DISTINCT u.id, u.username 
        FROM users u 
        JOIN posts p ON u.id = p.user_id 
        WHERE p.content LIKE ? 
        AND u.id NOT IN (SELECT user_id FROM group_members WHERE group_id = ?)
    ''', (keyword_search, group_id))
    
    # NEW: Check if the current logged-in user is already a member
    current_user_id = session.get('user_id')
    is_member = query_db('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', 
                         (group_id, current_user_id), one=True)
    
    return render_template('group_detail.html.j2', group=group, members=members, 
                           suggestions=suggestions, is_member=bool(is_member))

# 4. Route to add a suggested member to the group
@app.route('/groups/<int:group_id>/add_member', methods=['POST'])
def add_group_member(group_id):
    user_id = request.form.get('user_id')
    try:
        query_db('INSERT INTO group_members (group_id, user_id) VALUES (?, ?)', (group_id, user_id), commit=True)
    except sqlite3.IntegrityError:
        pass # Already a member
    return redirect(url_for('group_detail', group_id=group_id))

# 5. Route to leave a group
@app.route('/groups/<int:group_id>/leave', methods=['POST'])
def leave_group(group_id):
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    query_db('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', 
             (group_id, session['user_id']), commit=True)
    return redirect(url_for('group_detail', group_id=group_id))

# 6. Route to delete a group (Creator only)
@app.route('/groups/<int:group_id>/delete', methods=['POST'])
def delete_group(group_id):
    if not session.get('user_id'):
        return redirect(url_for('login'))
        
    group = query_db('SELECT creator_id FROM user_groups WHERE id = ?', (group_id,), one=True)
    
    # Only delete if the group exists and the current user is the creator
    if group and group['creator_id'] == session['user_id']:
        query_db('DELETE FROM user_groups WHERE id = ?', (group_id,), commit=True)
        query_db('DELETE FROM group_members WHERE group_id = ?', (group_id,), commit=True)
        
    return redirect(url_for('groups'))

# --------------------------------------------------------------
# --- DESIGN CLAIM 39: Pre-post warning API ---
@app.route('/api/check_content', methods=['POST'])
def check_content_api():
    """Receives text from the frontend, scores it, and returns the score."""
    if not session.get('user_id'):
        return {"score": 0, "moderated_text": ""}
        
    data = request.get_json()
    content = data.get('content', '')
    
    # Run it through the moderation function we just built
    moderated_text, score = moderate_content(content)
    
    return {"score": score, "moderated_text": moderated_text}

def recommend(user_id, following_only=False):
    """Returns 5 recommended posts based on followed users and positive reaction history."""
    db = get_db()
    query = '''
        SELECT DISTINCT p.id, p.content, p.created_at, u.username, p.user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.user_id != ? 
        AND (
            p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)
            OR p.user_id IN (
                SELECT p2.user_id FROM posts p2
                JOIN reactions r ON r.post_id = p2.id WHERE r.user_id = ? 
            )
        )
        AND p.id NOT IN (SELECT post_id FROM reactions WHERE user_id = ?)
        ORDER BY p.created_at DESC LIMIT 5
    '''
    posts_raw = db.execute(query, (user_id, user_id, user_id, user_id)).fetchall()
    recommended_posts = []
    for post in posts_raw:
        post_dict = dict(post)
        moderated_content, _ = moderate_content(post_dict['content'])
        post_dict['content'] = moderated_content
        recommended_posts.append(post_dict)
    return recommended_posts


@app.route('/sandbox', methods=['GET', 'POST'])
def sandbox():
    """Allows newcomers to test their posts against moderation rules safely."""
    user_id = session.get('user_id')
    if not user_id: return redirect(url_for('login'))
    result = None
    if request.method == 'POST':
        test_content = request.form.get('content')
        moderated_text, score = moderate_content(test_content)
        result = {'original': test_content, 'moderated': moderated_text, 'score': score}
    return render_template('sandbox.html.j2', result=result)

if __name__ == '__main__':
    app.run(debug=True, port=8080)

