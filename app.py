import os
import eventlet
eventlet.monkey_patch()
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import logging

# Suppress noisy ConnectionAbortedError/BrokenPipeError tracebacks from eventlet wsgi when a client aborts a request.
# These occur when the client cancels a range download (e.g. a partial audio file request) and are harmless.
class IgnoreSocketErrorsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # If there was an exception recorded, ignore specific socket-related errors
        if record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, (ConnectionAbortedError, BrokenPipeError)):
                return False
        # Also check the rendered message to avoid logging those errors
        msg = record.getMessage() if hasattr(record, 'getMessage') else str(record)
        if 'ConnectionAbortedError' in msg or 'BrokenPipeError' in msg:
            return False
        return True

# Attach filter to eventlet wsgi logger and root logger
logging.getLogger('eventlet.wsgi').addFilter(IgnoreSocketErrorsFilter())
logging.getLogger().addFilter(IgnoreSocketErrorsFilter())
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, UTC
import threading
import time
from db import db
import game

app = Flask(__name__)
app.config['SECRET_KEY'] = 'IRISH-2026'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if os.environ.get('RENDER'):
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL'].replace('postgres://', 'postgresql://', 1)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///irish2026.db'  # new schema for BUILT; old irish.db is left alone

# Now initialize your models with this same db
db.init_app(app)

from models import Team, TeamRanking, Match, MatchEvent

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Global timer variables
current_match_id = None
timer_thread = None
timer_running = False
field_fault_active = False

# Create database tables
with app.app_context():
    db.create_all()

def timer_payload(match):
    """What every screen needs each tick: clock, period, HUB side mapping."""
    period, clock, red_side = game.match_state(game.MATCH_LENGTH - match.match_time_remaining)
    return {
        'match_id': match.id,
        'time_remaining': clock,
        'period': period,
        'is_endgame': period == 'END GAME',
        'red_side_alliance': red_side,  # which alliance FUEL in the RED side of the HUB counts for
        'field_fault': field_fault_active,
    }

def match_timer():
    """Background timer: AUTO 0:20, 3s scoring delay, TELEOP 2:20 in one run."""
    global timer_running, current_match_id, field_fault_active

    with app.app_context():
        match = db.session.get(Match, current_match_id)
        if not match:
            return

        while timer_running and match.match_time_remaining > 0:
            time.sleep(1)

            # Skip timer countdown if field fault is active
            if field_fault_active:
                continue

            match.match_time_remaining -= 1
            payload = timer_payload(match)
            match.is_endgame = payload['is_endgame']
            db.session.commit()
            socketio.emit('timer_update', payload)

        # Match ended
        if timer_running:
            match.status = 'completed'
            match.end_time = datetime.now(UTC)
            calculate_rps(match)
            db.session.commit()
            socketio.emit('match_ended', {'match_id': current_match_id})

            # Wait 1 second then clear endgame indicator
            time.sleep(1)
            socketio.emit('clear_endgame', {'match_id': current_match_id})

def calculate_rps(match):
    """Set RP flags and update team rankings. Safe to call repeatedly:
    only the change since the last call is added to the teams."""
    counts_for_ranking = (match.match_type or '').lower() == 'qualification'
    for a in ('red', 'blue'):
        auto, fuel, _, total = game.ranking_points(match, a)
        setattr(match, f'{a}_auto_rp', auto)
        setattr(match, f'{a}_fuel_rp', fuel)
        total = total if counts_for_ranking else 0
        delta = total - (getattr(match, f'{a}_rp') or 0)
        setattr(match, f'{a}_rp', total)
        if delta:
            for t in (getattr(match, f'{a}_team1'), getattr(match, f'{a}_team2')):
                if t:
                    add_rps_to_team(t.id, delta)

    # Update rankings after distributing points
    update_rankings()

def recount(match):
    """Rebuild every count and both scores from the match's event log."""
    for a in ('red', 'blue'):
        for t in game.COUNT_TYPES:
            setattr(match, f'{a}_{t}', 0)
    match.connection = False
    for e in MatchEvent.query.filter_by(match_id=match.id).all():
        if e.event_type == 'connection':
            match.connection = True
        elif e.event_type in game.COUNT_TYPES and e.alliance in ('red', 'blue'):
            key = f'{e.alliance}_{e.event_type}'
            setattr(match, key, getattr(match, key) + 1)
    match.red_score = game.score(match, 'red')
    match.blue_score = game.score(match, 'blue')

def add_rps_to_team(team_id, rps):
    """Add ranking points to a team"""
    ranking = TeamRanking.query.filter_by(team_id=team_id).first()
    if not ranking:
        ranking = TeamRanking(team_id=team_id, ranking_points=0)
        db.session.add(ranking)
    ranking.ranking_points += rps
    db.session.commit()

def update_rankings():
    """Recalculate all team rankings and update their rank positions"""
    # Get all rankings ordered by points (descending)
    rankings = TeamRanking.query.order_by(TeamRanking.ranking_points.desc()).all()
    
    # Update previous_rank before changing current_rank
    for ranking in rankings:
        ranking.previous_rank = ranking.current_rank
    
    # Assign new ranks
    for idx, ranking in enumerate(rankings):
        ranking.current_rank = idx + 1
    
    db.session.commit()
    
    # Notify all clients that rankings have been updated
    socketio.emit('rankings_updated')

def get_rank_change_indicator(ranking):
    """Get the arrow indicator for rank change"""
    if ranking.previous_rank is None:
        return '—'  # Dash for first ranking
    elif ranking.current_rank < ranking.previous_rank:
        return '↑'  # Up arrow (lower rank number is better)
    elif ranking.current_rank > ranking.previous_rank:
        return '↓'  # Down arrow
    else:
        return '—'  # Dash for no change


@app.route('/')
def index():
    # Default to unified display
    return render_template('unified_display.html')

@app.route('/fta')
def fta():
    return render_template('fta_new.html')

@app.route('/referee')
def referee():
    return render_template('referee.html')

@app.route('/rankings')
def rankings():
    return render_template('rankings.html')

@app.route('/admin')
def admin_panel():
    return render_template('admin.html')

TEAM_KEYS = ('red_team1', 'red_team2', 'blue_team1', 'blue_team2')

def match_dict(m):
    """All match columns + team numbers, used by every match payload."""
    d = {c.name: getattr(m, c.name) for c in Match.__table__.columns
         if c.name not in ('start_time', 'end_time')}
    for k in TEAM_KEYS:
        team = getattr(m, k)
        d[k] = team.number if team else ''
    d['match_id'] = m.id
    d['red_win_rp'] = m.red_score > m.blue_score
    d['blue_win_rp'] = m.blue_score > m.red_score
    return d

@app.route('/api/current_match')
def get_current_match():
    # Get the most relevant match
    match = Match.query.filter_by(status='in_progress').first()
    if not match:
        match = Match.query.filter_by(status='scheduled').first()
    if not match:
        match = Match.query.order_by(Match.id.desc()).first()

    if match:
        return jsonify(match_dict(match))
    return jsonify({
        'match_number': 1,
        'match_type': 'Qualification',
        'red_team1': '',
        'red_team2': '',
        'blue_team1': '',
        'blue_team2': '',
        'red_score': 0,
        'blue_score': 0,
        'status': 'scheduled'
    })

@app.route('/api/matches')
def get_matches():
    out = []
    for m in Match.query.all():
        d = match_dict(m)
        for k in TEAM_KEYS:  # FTA/admin expect {'number': n} or null here
            d[k] = {'number': d[k]} if d[k] != '' else None
        out.append(d)
    return jsonify(out)

@app.route('/api/matches/<int:match_id>', methods=['DELETE'])
def delete_match(match_id):
    """Delete a match and all its associated events"""
    match = db.session.get(Match, match_id)
    if not match:
        return jsonify({'error': 'Match not found'}), 404
    
    # Delete all associated match events first
    MatchEvent.query.filter_by(match_id=match_id).delete()
    
    # Delete the match
    db.session.delete(match)
    db.session.commit()
    
    return jsonify({'success': True, 'message': f'Match {match_id} deleted'})

@app.route('/api/match_events/<int:match_id>')
def get_match_events(match_id):
    """Get all scoring events for a specific match"""
    events = MatchEvent.query.filter_by(match_id=match_id).order_by(MatchEvent.timestamp.asc()).all()
    return jsonify([{
        'id': e.id,
        'match_id': e.match_id,
        'event_type': e.event_type,
        'alliance': e.alliance,
        'points': e.points,
        'timestamp': e.timestamp.isoformat() if e.timestamp else None,
        'details': e.details
    } for e in events])

@app.route('/api/rankings')
def get_rankings():
    """Get all teams ranked by ranking points with their rank change indicators"""
    rankings = TeamRanking.query.order_by(TeamRanking.ranking_points.desc()).all()
    return jsonify([{
        'team_number': ranking.team.number,
        'team_name': ranking.team.name,
        'ranking_points': ranking.ranking_points,
        'rank': ranking.current_rank,
        'rank_change': get_rank_change_indicator(ranking)
    } for ranking in rankings])

@app.route('/api/team_rank/<int:team_number>')
def get_team_rank(team_number):
    """Get rank information for a specific team"""
    team = Team.query.filter_by(number=team_number).first()
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    
    ranking = TeamRanking.query.filter_by(team_id=team.id).first()
    if not ranking:
        return jsonify({
            'team_number': team_number,
            'rank': None,
            'ranking_points': 0,
            'rank_change': '—'
        })
    
    return jsonify({
        'team_number': team_number,
        'rank': ranking.current_rank,
        'ranking_points': ranking.ranking_points,
        'rank_change': get_rank_change_indicator(ranking)
    })

@app.route('/api/teams', methods=['GET', 'POST'])
def manage_teams():
    """Get all teams or add a new team"""
    if request.method == 'GET':
        teams = Team.query.order_by(Team.number).all()
        return jsonify([{
            'id': t.id,
            'number': t.number,
            'name': t.name
        } for t in teams])
    
    elif request.method == 'POST':
        data = request.json
        number = data.get('number')
        name = data.get('name')
        
        if not number or not name:
            return jsonify({'error': 'Team number and name are required'}), 400
        
        # Check if team number already exists
        existing = Team.query.filter_by(number=number).first()
        if existing:
            return jsonify({'error': f'Team {number} already exists'}), 400
        
        team = Team(number=number, name=name)
        db.session.add(team)
        db.session.commit()
        
        return jsonify({
            'id': team.id,
            'number': team.number,
            'name': team.name,
            'message': 'Team added successfully'
        }), 201

@app.route('/api/teams/<int:team_id>', methods=['PUT', 'DELETE'])
def modify_team(team_id):
    """Update or delete a team"""
    team = db.session.get(Team, team_id)
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    
    if request.method == 'PUT':
        data = request.json
        new_name = data.get('name')
        
        if not new_name:
            return jsonify({'error': 'Team name is required'}), 400
        
        team.name = new_name
        db.session.commit()
        
        return jsonify({
            'id': team.id,
            'number': team.number,
            'name': team.name,
            'message': 'Team updated successfully'
        })
    
    elif request.method == 'DELETE':
        # Check if team has matches
        matches_count = Match.query.filter(
            (Match.red_team1_id == team_id) |
            (Match.red_team2_id == team_id) |
            (Match.blue_team1_id == team_id) |
            (Match.blue_team2_id == team_id)
        ).count()
        
        if matches_count > 0:
            return jsonify({
                'error': f'Cannot delete team. Team has {matches_count} associated match(es). Delete matches first.'
            }), 400
        
        # Delete associated ranking if exists
        TeamRanking.query.filter_by(team_id=team_id).delete()
        
        db.session.delete(team)
        db.session.commit()
        
        return jsonify({'message': 'Team deleted successfully'})

# ========== ADMIN API ENDPOINTS ==========

@app.route('/api/admin/matches/<int:match_id>', methods=['PUT'])
def admin_update_match(match_id):
    """Admin endpoint to update match details"""
    match = db.session.get(Match, match_id)
    if not match:
        return jsonify({'error': 'Match not found'}), 404
    
    data = request.json
    
    # Update match fields
    if 'match_number' in data:
        match.match_number = data['match_number']
    if 'match_type' in data:
        match.match_type = data['match_type']
    if 'status' in data:
        match.status = data['status']
    if 'red_score' in data:
        match.red_score = data['red_score']
    if 'blue_score' in data:
        match.blue_score = data['blue_score']
    
    # Update teams by team number
    if 'red_team1' in data and data['red_team1']:
        team = Team.query.filter_by(number=data['red_team1']).first()
        match.red_team1_id = team.id if team else None
    
    if 'red_team2' in data and data['red_team2']:
        team = Team.query.filter_by(number=data['red_team2']).first()
        match.red_team2_id = team.id if team else None
    
    if 'blue_team1' in data and data['blue_team1']:
        team = Team.query.filter_by(number=data['blue_team1']).first()
        match.blue_team1_id = team.id if team else None
    
    if 'blue_team2' in data and data['blue_team2']:
        team = Team.query.filter_by(number=data['blue_team2']).first()
        match.blue_team2_id = team.id if team else None
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Match updated successfully',
        'match_id': match.id
    })

@app.route('/api/admin/matches/all', methods=['DELETE'])
def admin_delete_all_matches():
    """Admin endpoint to delete ALL matches and events"""
    try:
        # Delete all match events
        MatchEvent.query.delete()
        # Delete all matches
        Match.query.delete()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'All matches and events deleted'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/rankings/<team_number>', methods=['PUT'])
def admin_update_ranking(team_number):
    """Admin endpoint to update team ranking points"""
    team = Team.query.filter_by(number=team_number).first()
    if not team:
        return jsonify({'error': 'Team not found'}), 404
    
    ranking = TeamRanking.query.filter_by(team_id=team.id).first()
    if not ranking:
        return jsonify({'error': 'Ranking not found'}), 404
    
    data = request.json
    if 'ranking_points' not in data:
        return jsonify({'error': 'ranking_points is required'}), 400
    
    ranking.ranking_points = data['ranking_points']
    db.session.commit()
    
    # Recalculate all rankings
    update_rankings()
    
    return jsonify({
        'success': True,
        'message': 'Ranking updated successfully',
        'team_number': team_number,
        'ranking_points': ranking.ranking_points
    })

@app.route('/api/admin/rankings/reset', methods=['POST'])
def admin_reset_rankings():
    """Admin endpoint to reset all rankings to 0"""
    try:
        rankings = TeamRanking.query.all()
        for ranking in rankings:
            ranking.ranking_points = 0
            ranking.previous_rank = 0
            ranking.current_rank = 0
        
        db.session.commit()
        update_rankings()
        
        return jsonify({
            'success': True,
            'message': 'All rankings reset to 0'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/events')
def admin_get_events():
    """Admin endpoint to get all match events with optional filtering"""
    match_id = request.args.get('match_id', type=int)
    
    query = MatchEvent.query
    if match_id:
        query = query.filter_by(match_id=match_id)
    
    events = query.order_by(MatchEvent.timestamp.desc()).all()
    
    return jsonify([{
        'id': e.id,
        'match_id': e.match_id,
        'alliance': e.alliance,
        'event_type': e.event_type,
        'points': e.points,
        'timestamp': e.timestamp.isoformat() if e.timestamp else None
    } for e in events])

@app.route('/api/admin/events/<int:event_id>', methods=['DELETE'])
def admin_delete_event(event_id):
    """Admin endpoint to delete a match event"""
    event = db.session.get(MatchEvent, event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    match = db.session.get(Match, event.match_id)
    db.session.delete(event)
    db.session.flush()
    if match:
        recount(match)  # keep the score in sync with the event log
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Event deleted successfully'
    })

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('score_event')
def handle_score_event(data):
    """One scoring tap. Live FUEL is sent by HUB side ('hub_side': red/blue) and the
    server works out alliance + AUTO/TELEOP from the clock. Everything else (and
    review-mode FUEL) is sent with an explicit alliance and event_type."""
    match = db.session.get(Match, data.get('match_id'))
    if not match:
        return

    alliance = data.get('alliance')
    event_type = data.get('event_type')
    details = None
    if event_type == 'fuel':
        side = data.get('hub_side')
        if side not in ('red', 'blue'):
            return
        alliance, event_type = game.credit_fuel(side, game.MATCH_LENGTH - match.match_time_remaining)
        details = f'{side} side of HUB'

    if event_type == 'connection':
        alliance, points = None, 0
    elif event_type in game.COUNT_TYPES and alliance in ('red', 'blue'):
        points = game.POINTS.get(event_type) or game.FOUL_POINTS[event_type]
    else:
        return

    event = MatchEvent(match_id=match.id, event_type=event_type,
                       alliance=alliance, points=points, details=details)
    db.session.add(event)
    db.session.flush()
    recount(match)
    db.session.commit()
    emit('event_recorded', {'event_id': event.id})  # sender only, powers its Undo button

    # Broadcast score update to all clients (display, referee, FTA)
    socketio.emit('score_updated', match_dict(match))

@socketio.on('fta_field_fault')
def handle_field_fault(data):
    """Pause the match timer for a field fault"""
    global field_fault_active
    field_fault_active = True
    socketio.emit('field_fault_started', {'match_id': current_match_id})
    # Immediately notify displays of timer state so banner shows without waiting for next tick
    match = db.session.get(Match, current_match_id)
    if match:
        socketio.emit('timer_update', timer_payload(match))

@socketio.on('fta_resume_match')
def handle_resume_match(data):
    """Resume the match timer after a field fault"""
    global field_fault_active
    field_fault_active = False
    socketio.emit('field_fault_ended', {'match_id': current_match_id})
    # Immediately notify displays of timer state so banner hides without waiting for next tick
    match = db.session.get(Match, current_match_id)
    if match:
        socketio.emit('timer_update', timer_payload(match))

def get_next_match_number():
    with app.app_context():
        last = db.session.query(db.func.max(Match.match_number)).scalar()
        return (last or 0) + 1
    
@app.route('/api/next_match_number')
def api_next_match_number():
    return jsonify({'next_match_number': get_next_match_number()})

# FTA Socket Handlers
@socketio.on('fta_save_match')
def handle_fta_save_match(data):
    match_type = data.get('match_type', 'Qualification')  # Get match_type with default
    match_id = data.get('match_id')
    match_number = data.get('match_number')
    red_team1_num = data.get('red_team1')
    red_team2_num = data.get('red_team2')
    blue_team1_num = data.get('blue_team1')
    blue_team2_num = data.get('blue_team2')

    if match_number:
        try:
            match_number = int(match_number)
        except Exception:
            match_number = get_next_match_number()
    else:
        match_number = get_next_match_number()

    # If no match_id provided, create a new match
    if not match_id:
        match = Match(match_number=match_number, match_type=match_type)  # Add match_type
    else:
        match = db.session.get(Match, match_id)
        if not match:
            match = Match(match_number=match_number, match_type=match_type)  # Add match_type
        else:
            match.match_type = match_type  # Update match_type

    # Auto-create teams if they don't exist and assign them
    if red_team1_num and red_team1_num.strip():
        red_team1 = Team.query.filter_by(number=int(red_team1_num)).first()
        if not red_team1:
            red_team1 = Team(number=int(red_team1_num), name=f'Team {red_team1_num}')
            db.session.add(red_team1)
        match.red_team1 = red_team1
    else:
        match.red_team1 = None
        
    if red_team2_num and red_team2_num.strip():
        red_team2 = Team.query.filter_by(number=int(red_team2_num)).first()
        if not red_team2:
            red_team2 = Team(number=int(red_team2_num), name=f'Team {red_team2_num}')
            db.session.add(red_team2)
        match.red_team2 = red_team2
    else:
        match.red_team2 = None
        
    if blue_team1_num and blue_team1_num.strip():
        blue_team1 = Team.query.filter_by(number=int(blue_team1_num)).first()
        if not blue_team1:
            blue_team1 = Team(number=int(blue_team1_num), name=f'Team {blue_team1_num}')
            db.session.add(blue_team1)
        match.blue_team1 = blue_team1
    else:
        match.blue_team1 = None
        
    if blue_team2_num and blue_team2_num.strip():
        blue_team2 = Team.query.filter_by(number=int(blue_team2_num)).first()
        if not blue_team2:
            blue_team2 = Team(number=int(blue_team2_num), name=f'Team {blue_team2_num}')
            db.session.add(blue_team2)
        match.blue_team2 = blue_team2
    else:
        match.blue_team2 = None

    # Set match number if provided
    if match_number:
        match.match_number = match_number

    # Add to database if it's a new match
    if not match.id:
        db.session.add(match)

    db.session.commit()

    emit('match_data_saved', {'match_id': match.id})

@socketio.on('fta_ready_display')
def handle_fta_ready_display():
    # Update display to show "Ready for Match" screen
    socketio.emit('show_ready_for_match')

@socketio.on('fta_update_display')
def handle_fta_update_display(data):
    # Send data directly to display to show in prematch
    socketio.emit('show_prematch', {
        'match_type': data.get('match_type', '?'),
        'match_number': data.get('match_number', '?'),
        'red_team1': data.get('red_team1', '????'),
        'red_team2': data.get('red_team2', '????'),
        'blue_team1': data.get('blue_team1', '????'),
        'blue_team2': data.get('blue_team2', '????')
    })

@socketio.on('fta_start_match')
def handle_fta_start_match(data):
    global current_match_id, timer_thread, timer_running, field_fault_active

    match_id = data.get('match_id')
    match_type = data.get('match_type', 'Qualification')
    quick_start = data.get('quick_start', False)  # jump to the last 15s of TELEOP

    match = db.session.get(Match, match_id) if match_id else None
    if not match:
        supplied_num = data.get('match_number')
        try:
            match_number = int(supplied_num)
        except (TypeError, ValueError):
            match_number = get_next_match_number()
        match = Match(match_number=match_number)
        db.session.add(match)

    # Assign teams - auto-create them if they don't exist
    for team_key in TEAM_KEYS:
        team_num = data.get(team_key)
        if team_num and str(team_num).strip():
            team = Team.query.filter_by(number=int(team_num)).first()
            if not team:
                team = Team(number=int(team_num), name=f'Team {team_num}')
                db.session.add(team)
            setattr(match, team_key, team)
        elif not match_id:
            setattr(match, team_key, None)

    match.match_type = match_type
    match.status = 'in_progress'
    match.start_time = datetime.now(UTC)
    match.match_time_remaining = 15 if quick_start else game.MATCH_LENGTH
    db.session.flush()

    # A (re)start is a fresh match: drop events left over from an accidental start
    MatchEvent.query.filter_by(match_id=match.id).delete()
    recount(match)
    match.is_endgame = timer_payload(match)['is_endgame']
    db.session.commit()

    current_match_id = match.id
    field_fault_active = False
    timer_running = True
    timer_thread = threading.Thread(target=match_timer)
    timer_thread.daemon = True
    timer_thread.start()

    payload = match_dict(match)
    socketio.emit('show_live', payload)
    socketio.emit('timer_update', timer_payload(match))
    payload['status'] = 'in_progress'
    socketio.emit('match_started', payload)

@socketio.on('fta_finalize_match')
def handle_fta_finalize_match(data):
    """Finalize match after review - recalculate RPs with reviewed scores"""
    match_id = data.get('match_id')
    match = db.session.get(Match, match_id)
    if match:
        # Mark match as finalized
        match.status = 'finalized'
        
        # Recalculate ranking points with final reviewed scores
        # (scores have already been updated by score_event/delete_event handlers)
        calculate_rps(match)
        db.session.commit()
        
        print(f"Match {match_id} finalized with reviewed scores.")
        print(f"  Red: {match.red_score} pts, RPs: {match.red_rp}")
        print(f"  Blue: {match.blue_score} pts, RPs: {match.blue_rp}")
        
        # Notify all clients that match is finalized
        socketio.emit('match_finalized', {
            'match_id': match_id,
            'red_score': match.red_score,
            'blue_score': match.blue_score
        })

@socketio.on('fta_show_review')
def handle_fta_show_review(data=None):
    # Show review banner on live display and send match_id to referee
    match_id = data.get('match_id') if data else None
    socketio.emit('show_review', {'match_id': match_id})

@socketio.on('fta_hide_review')
def handle_fta_hide_review(data=None):
    # Hide review banner on live display
    socketio.emit('hide_review')

@socketio.on('review_complete')
def handle_review_complete(data):
    """Broadcast that review is complete and showcase can be enabled"""
    match_id = data.get('match_id')
    socketio.emit('review_complete', {'match_id': match_id})

@socketio.on('delete_event')
def handle_delete_event(data):
    """Delete a scoring event and recalculate match scores"""
    event = db.session.get(MatchEvent, data.get('event_id'))
    match_id = data.get('match_id')
    if not event or event.match_id != match_id:
        return
    match = db.session.get(Match, match_id)
    db.session.delete(event)
    db.session.flush()
    recount(match)
    db.session.commit()

    payload = match_dict(match)
    socketio.emit('event_deleted', payload)
    socketio.emit('score_updated', payload)

@socketio.on('fta_show_postmatch')
def handle_fta_show_postmatch(data):
    match_id = data.get('match_id')
    if match_id:
        match = db.session.get(Match, match_id)
    else:
        # Get most recent match
        match = Match.query.order_by(Match.id.desc()).first()
    if not match:
        print(f"Match ID {match_id} not found for postmatch display.")
        return

    # Ensure match is marked as completed and saved to DB
    if match.status not in ('completed', 'finalized'):
        match.status = 'completed'
        if not match.end_time:
            match.end_time = datetime.now(UTC)
    calculate_rps(match)
    db.session.commit()

    socketio.emit('show_postmatch', match_dict(match))

@socketio.on('load_match_data')
def handle_load_match_data(data):
    match_id = data.get('match_id')

    if not match_id:
        # No match ID provided, emit empty data
        emit('match_data_loaded', {
            'match_id': None,
            'match_type': 'Qualification',  # Add this line
            'match_number': 1,
            'red_team1': '',
            'red_team2': '',
            'blue_team1': '',
            'blue_team2': '',
            'status': 'scheduled'
        })
        return

    match = db.session.get(Match, match_id)
    if match:
        emit('match_data_loaded', {
            'match_id': match.id,
            'match_type': match.match_type,  # Add this line
            'match_number': match.match_number,
            'red_team1': match.red_team1.number if match.red_team1 else '',
            'red_team2': match.red_team2.number if match.red_team2 else '',
            'blue_team1': match.blue_team1.number if match.blue_team1 else '',
            'blue_team2': match.blue_team2.number if match.blue_team2 else '',
            'status': match.status
        })
    else:
        # Match not found, emit empty data
        emit('match_data_loaded', {
            'match_id': None,
            'match_type': 'Qualification',  # Add this line
            'match_number': 1,
            'red_team1': '',
            'red_team2': '',
            'blue_team1': '',
            'blue_team2': '',
            'status': 'scheduled'
        })

@socketio.on('fta_stop_match')
def handle_fta_stop_match(data):
    """Stop the current match and reset timer"""
    global timer_running, current_match_id
    match_id = data.get('match_id') or current_match_id
    
    if not match_id:
        print("Warning: No match_id provided and no current match is running")
        return
    
    match = db.session.get(Match, match_id)
    if match:
        timer_running = False
        match.status = 'stopped'
        match.match_time_remaining = 0
        match.is_endgame = False
        db.session.commit()
        socketio.emit('match_stopped', {'match_id': match_id})
        print(f"Match {match_id} stopped successfully")

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
else:
    socketio.run(
        app,
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        allow_unsafe_werkzeug=True
    )