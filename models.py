from flask_sqlalchemy import SQLAlchemy
from db import db
from game import MATCH_LENGTH


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)

class TeamRanking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False, unique=True)
    ranking_points = db.Column(db.Integer, default=0)
    previous_rank = db.Column(db.Integer, nullable=True)  # Track previous rank for comparison
    current_rank = db.Column(db.Integer, nullable=True)
    
    team = db.relationship('Team', backref=db.backref('ranking', uselist=False))

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    match_number = db.Column(db.Integer, nullable=False)
    match_type = db.Column(db.String(20), default='Qualification') # qualification, playoff, final
    red_team1_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    red_team2_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    blue_team1_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    blue_team2_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)

    # Scores
    red_score = db.Column(db.Integer, default=0)
    blue_score = db.Column(db.Integer, default=0)

    # Match status and timing
    status = db.Column(db.String(20), default='scheduled')  # scheduled, in_progress, completed
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    match_time_remaining = db.Column(db.Integer, default=MATCH_LENGTH)  # ticks left of AUTO+delay+TELEOP
    is_endgame = db.Column(db.Boolean, default=False)

    # Scoring counts per alliance (point values live in game.py)
    red_auto_fuel = db.Column(db.Integer, default=0)
    red_teleop_fuel = db.Column(db.Integer, default=0)
    red_leave = db.Column(db.Integer, default=0)
    red_park = db.Column(db.Integer, default=0)
    red_minor_foul = db.Column(db.Integer, default=0)   # committed BY red, credited to blue
    red_major_foul = db.Column(db.Integer, default=0)
    blue_auto_fuel = db.Column(db.Integer, default=0)
    blue_teleop_fuel = db.Column(db.Integer, default=0)
    blue_leave = db.Column(db.Integer, default=0)
    blue_park = db.Column(db.Integer, default=0)
    blue_minor_foul = db.Column(db.Integer, default=0)
    blue_major_foul = db.Column(db.Integer, default=0)
    connection = db.Column(db.Boolean, default=False)  # all 4 robots; both alliances earn the RP

    # RP results (set by calculate_rps)
    red_auto_rp = db.Column(db.Boolean, default=False)
    red_fuel_rp = db.Column(db.Boolean, default=False)
    blue_auto_rp = db.Column(db.Boolean, default=False)
    blue_fuel_rp = db.Column(db.Boolean, default=False)
    red_rp = db.Column(db.Integer, default=0)   # RP already added to team rankings
    blue_rp = db.Column(db.Integer, default=0)

    red_team1 = db.relationship('Team', foreign_keys=[red_team1_id])
    red_team2 = db.relationship('Team', foreign_keys=[red_team2_id])
    blue_team1 = db.relationship('Team', foreign_keys=[blue_team1_id])
    blue_team2 = db.relationship('Team', foreign_keys=[blue_team2_id])

class MatchEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)  # score_update, foul, timer, etc.
    alliance = db.Column(db.String(10))  # 'red' or 'blue'
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'))
    points = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    details = db.Column(db.String(200))  # Additional details like scoring type

    match = db.relationship('Match', backref=db.backref('events', lazy=True))
    team = db.relationship('Team')