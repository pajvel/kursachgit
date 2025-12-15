from itertools import groupby

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Min
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.db import connection
from django.http import HttpResponse
import io

from .models import Team, Player, TeamPlayer, Match, MatchLineup, MatchEvent


# ============================================================
#                    ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_match_score(match):
    """
    Возвращает (home_goals, away_goals) по событиям матча.
    Учитываются:
      - 'гол'
      - 'пенальти_гол'
      - 'автогол' (в пользу соперника)
    """
    scoring_types = ['гол', 'пенальти_гол']
    own_goal_type = 'автогол'

    events = MatchEvent.objects.filter(match=match)

    home_goals_normal = events.filter(
        team=match.home_team,
        event_type__in=scoring_types,
    ).count()
    away_goals_normal = events.filter(
        team=match.away_team,
        event_type__in=scoring_types,
    ).count()

    home_from_own = events.filter(
        team=match.away_team,
        event_type=own_goal_type,
    ).count()
    away_from_own = events.filter(
        team=match.home_team,
        event_type=own_goal_type,
    ).count()

    return home_goals_normal + home_from_own, away_goals_normal + away_from_own


def get_header_matches():
    """
    Матчи для горизонтального блока под шапкой:
    - несколько последних завершённых
    - несколько ближайших (идёт / запланирован)
    Для завершённых / идущих считаем счёт по событиям матча.
    """
    now = timezone.now()

    # 5 последних завершённых
    past_qs = (
        Match.objects
        .filter(status='завершён', date__lte=now)
        .select_related('home_team', 'away_team')
        .order_by('-date')[:5]
    )

    # 5 ближайших:
    # - все "идёт" независимо от даты
    # - "запланирован" только с датой >= now
    future_qs = (
        Match.objects
        .filter(
            Q(status='идёт') |
            Q(status='запланирован', date__gte=now)
        )
        .select_related('home_team', 'away_team')
        .order_by('date')[:5]
    )

    matches = list(past_qs)
    matches.reverse()
    matches += list(future_qs)

    for m in matches:
        if m.status in ['завершён', 'идёт']:
            m.home_goals, m.away_goals = get_match_score(m)
        else:
            m.home_goals = None
            m.away_goals = None

    return matches


def sort_team_players_by_position(team_players):
    """
    team_players: queryset/teamplayers list, где есть tp.player.position
    Сортируем: GK -> DEF -> MID -> FWD, а внутри группы по фамилии/имени.
    """
    pos_rank = {
        'ВРТ': 0, 'Вратарь': 0,
        'ЗАЩ': 1, 'Защитник': 1,
        'ПЗ': 2, 'Полузащитник': 2,
        'НАП': 3, 'Нападающий': 3,
    }

    def key(tp):
        p = tp.player
        pos = (p.position or '').upper()
        rank = pos_rank.get(pos, 99)  # неизвестные в конец
        last = (p.last_name or '')
        first = (p.first_name or '')
        return (rank, last, first)

    return sorted(team_players, key=key)


def _calculate_standings():
    """Считаем турнирную таблицу по завершённым матчам и голевым событиям (учитываем автоголы)."""
    teams = Team.objects.all()
    stats = {
        t.id: {
            'team': t,
            'games': 0,
            'wins': 0,
            'draws': 0,
            'losses': 0,
            'goals_for': 0,
            'goals_against': 0,
            'points': 0,
        }
        for t in teams
    }

    finished_matches = Match.objects.filter(status='завершён')

    for m in finished_matches:
        home_goals, away_goals = get_match_score(m)

        hs = stats[m.home_team_id]
        gs = stats[m.away_team_id]

        hs['games'] += 1
        gs['games'] += 1

        hs['goals_for'] += home_goals
        hs['goals_against'] += away_goals

        gs['goals_for'] += away_goals
        gs['goals_against'] += home_goals

        if home_goals > away_goals:
            hs['wins'] += 1
            hs['points'] += 3
            gs['losses'] += 1
        elif home_goals < away_goals:
            gs['wins'] += 1
            gs['points'] += 3
            hs['losses'] += 1
        else:
            hs['draws'] += 1
            gs['draws'] += 1
            hs['points'] += 1
            gs['points'] += 1

    table = sorted(
        stats.values(),
        key=lambda r: (
            -r['points'],
            -(r['goals_for'] - r['goals_against']),
            -r['goals_for'],
        ),
    )
    return table


def _get_team_row_from_table(team, standings):
    for row in standings:
        if row['team'].id == team.id:
            return row
    return None


# ============================================================
#                           ФОРМЫ
# ============================================================

class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ['name', 'city', 'coach', 'emblem']

    def clean_coach(self):
        """
        Один тренер может быть только у одной команды.
        Для ForeignKey сравниваем по exact (тот же объект).
        """
        coach = self.cleaned_data.get('coach')
        if coach:
            qs = Team.objects.filter(coach=coach)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError('Этот тренер уже привязан к другой команде.')
        return coach


class PlayerForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = ['first_name', 'last_name', 'birth_date', 'position']


class MatchForm(forms.ModelForm):
    class Meta:
        model = Match
        fields = ['home_team', 'away_team', 'date', 'status']


# ============================================================
#                           ГЛАВНАЯ
# ============================================================

def index(request):
    header_matches = get_header_matches()
    standings = _calculate_standings()

    scoring_types = ['гол', 'пенальти_гол']

    base_qs = (
        Player.objects
        .annotate(
            goals=Count(
                'events',
                filter=Q(events__event_type__in=scoring_types)
            ),
            assists=Count(
                'events',
                filter=Q(events__event_type='ассист')
            ),
            yellow_cards=Count(
                'events',
                filter=Q(events__event_type='желтая')
            ),
            red_cards=Count(
                'events',
                filter=Q(events__event_type='красная')
            ),
            games=Count('events__match', distinct=True),
        )
    )

    top_scorers = (
        base_qs
        .filter(goals__gt=0)
        .order_by('-goals', '-assists', 'last_name', 'first_name')[:5]
    )

    top_assists = (
        base_qs
        .filter(assists__gt=0)
        .order_by('-assists', '-goals', 'last_name', 'first_name')[:5]
    )

    top_yellow = (
        base_qs
        .filter(yellow_cards__gt=0)
        .order_by('-yellow_cards', 'last_name', 'first_name')[:5]
    )

    top_red = (
        base_qs
        .filter(red_cards__gt=0)
        .order_by('-red_cards', 'last_name', 'first_name')[:5]
    )

    context = {
        'header_matches': header_matches,
        'standings': standings,
        'top_scorers': top_scorers,
        'top_assists': top_assists,
        'top_yellow': top_yellow,
        'top_red': top_red,
    }
    return render(request, 'index.html', context)


# ============================================================
#                           КОМАНДЫ
# ============================================================

def team_list(request):
    header_matches = get_header_matches()

    search = request.GET.get('search', '').strip()
    sort = request.GET.get('sort', 'name')
    order = request.GET.get('order', 'asc')

    teams = Team.objects.all()

    if search:
        teams = teams.filter(
            Q(name__icontains=search) |
            Q(city__icontains=search)
        )

    if sort not in ['name', 'city']:
        sort = 'name'

    if order == 'desc':
        sort = '-' + sort

    teams = teams.order_by(sort)

    context = {
        'header_matches': header_matches,
        'teams': teams,
        'search': search,
        'sort': request.GET.get('sort', 'name'),
        'order': request.GET.get('order', 'asc'),
    }
    return render(request, 'team_list.html', context)


def team_detail(request, team_id):
    header_matches = get_header_matches()
    team = get_object_or_404(Team, pk=team_id)

    standings = _calculate_standings()
    team_row = _get_team_row_from_table(team, standings)

    squad_qs = (
        TeamPlayer.objects
        .filter(team=team)
        .select_related('player')
        .order_by('number', 'player__last_name')
    )

    player_ids = [tp.player_id for tp in squad_qs]
    scoring_types = ['гол', 'пенальти_гол']

    events_agg = (
        MatchEvent.objects
        .filter(team=team, player_id__in=player_ids)
        .values('player_id')
        .annotate(
            goals=Count('id', filter=Q(event_type__in=scoring_types)),
            assists=Count('id', filter=Q(event_type='ассист')),
            yellow=Count('id', filter=Q(event_type='желтая')),
            red=Count('id', filter=Q(event_type='красная')),
        )
    )

    events_map = {
        row['player_id']: {
            'goals': row['goals'],
            'assists': row['assists'],
            'yellow': row['yellow'],
            'red': row['red'],
        }
        for row in events_agg
    }

    starters_qs = (
        MatchLineup.objects
        .filter(team=team, player_id__in=player_ids, is_starting=True)
        .values('player_id', 'match_id')
        .distinct()
    )

    subs_qs = (
        MatchEvent.objects
        .filter(
            team=team,
            player_id__in=player_ids,
            event_type='замена'
        )
        .values('player_id', 'match_id')
        .distinct()
    )

    games_map = {pid: set() for pid in player_ids}

    for row in starters_qs:
        games_map.setdefault(row['player_id'], set()).add(row['match_id'])

    for row in subs_qs:
        games_map.setdefault(row['player_id'], set()).add(row['match_id'])

    squad = []
    for tp in squad_qs:
        pid = tp.player_id
        ev = events_map.get(pid, {'goals': 0, 'assists': 0, 'yellow': 0, 'red': 0})
        games = len(games_map.get(pid, set()))
        squad.append({
            'tp': tp,
            'number': tp.number,
            'games': games,
            'goals': ev['goals'],
            'assists': ev['assists'],
            'yellow': ev['yellow'],
            'red': ev['red'],
        })

    matches = (
        Match.objects
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related('home_team', 'away_team')
        .order_by('-date')
    )

    for m in matches:
        if m.status in ['завершён', 'идёт']:
            m.home_goals, m.away_goals = get_match_score(m)
        else:
            m.home_goals = None
            m.away_goals = None

    context = {
        'header_matches': header_matches,
        'team': team,
        'team_row': team_row,
        'squad': squad,
        'matches': matches,
    }
    return render(request, 'team_detail.html', context)


def team_create(request):
    header_matches = get_header_matches()

    if request.method == 'POST':
        form = TeamForm(request.POST)
        if form.is_valid():
            team = form.save()
            return redirect('team_detail', team_id=team.id)
    else:
        form = TeamForm()

    return render(request, 'team_create.html', {
        'header_matches': header_matches,
        'form': form,
    })


def team_edit(request, team_id):
    """
    Редактирование ТОЛЬКО данных команды (название, город, тренер, эмблема).
    Состав редактируется отдельно во view team_squad_edit.
    """
    header_matches = get_header_matches()
    team = get_object_or_404(Team, pk=team_id)

    if request.method == 'POST':
        form = TeamForm(request.POST, instance=team)
        if form.is_valid():
            form.save()
            return redirect('team_detail', team_id=team.id)
    else:
        form = TeamForm(instance=team)

    context = {
        'header_matches': header_matches,
        'team': team,
        'form': form,
    }
    return render(request, 'team_edit.html', context)


def team_squad_edit(request, team_id):
    """
    Отдельный редактор СОСТАВА команды:
    - изменение номеров
    - удаление игроков из команды
    - добавление новых игроков без команды
    - КАЖДЫЙ номер уникален внутри команды
    """
    header_matches = get_header_matches()
    team = get_object_or_404(Team, pk=team_id)

    squad_qs = TeamPlayer.objects.filter(team=team).select_related('player')
    available_players = Player.objects.exclude(team_players__isnull=False).order_by('last_name')

    if request.method == 'POST':
        remove_ids = set(request.POST.getlist('remove_tp'))

        updates = []
        for tp in squad_qs:
            number_key = f'number_{tp.id}'
            num_val = request.POST.get(number_key, '').strip()

            if str(tp.id) in remove_ids:
                updates.append((tp, None, True))
                continue

            if num_val == '':
                new_num = None
            else:
                try:
                    new_num = int(num_val)
                except ValueError:
                    new_num = None

            updates.append((tp, new_num, False))

        new_player_id = request.POST.get('new_player_id')
        new_player_number_raw = request.POST.get('new_player_number', '').strip()
        new_player_number = None
        if new_player_number_raw:
            try:
                new_player_number = int(new_player_number_raw)
            except ValueError:
                new_player_number = None

        used_numbers = set()
        for tp, new_num, remove_flag in updates:
            if remove_flag:
                continue
            if new_num is None:
                continue
            if new_num in used_numbers:
                error_message = (
                    f'В команде не может быть два игрока под номером {new_num}. '
                    f'Исправь номера и попробуй снова.'
                )
                squad_qs = TeamPlayer.objects.filter(team=team).select_related('player')
                available_players = Player.objects.exclude(team_players__isnull=False).order_by('last_name')
                context = {
                    'header_matches': header_matches,
                    'team': team,
                    'squad': squad_qs,
                    'available_players': available_players,
                    'error_message': error_message,
                }
                return render(request, 'team_squad_edit.html', context)
            used_numbers.add(new_num)

        if new_player_id and new_player_number is not None:
            if new_player_number in used_numbers:
                error_message = (
                    f'Игрок с номером {new_player_number} уже есть в этой команде. '
                    f'Выбери другой номер.'
                )
                squad_qs = TeamPlayer.objects.filter(team=team).select_related('player')
                available_players = Player.objects.exclude(team_players__isnull=False).order_by('last_name')
                context = {
                    'header_matches': header_matches,
                    'team': team,
                    'squad': squad_qs,
                    'available_players': available_players,
                    'error_message': error_message,
                }
                return render(request, 'team_squad_edit.html', context)

        for tp, new_num, remove_flag in updates:
            if remove_flag:
                tp.delete()
                continue
            tp.number = new_num
            tp.save()

        if new_player_id:
            player = Player.objects.filter(pk=new_player_id).first()
            if player and not TeamPlayer.objects.filter(player=player).exists():
                tp = TeamPlayer(team=team, player=player)
                if new_player_number is not None:
                    tp.number = new_player_number
                tp.save()

        return redirect('team_squad_edit', team_id=team.id)

    squad_qs = TeamPlayer.objects.filter(team=team).select_related('player')
    available_players = Player.objects.exclude(team_players__isnull=False).order_by('last_name')

    context = {
        'header_matches': header_matches,
        'team': team,
        'squad': squad_qs,
        'available_players': available_players,
    }
    return render(request, 'team_squad_edit.html', context)


def team_delete(request, team_id):
    header_matches = get_header_matches()
    team = get_object_or_404(Team, pk=team_id)

    if request.method == 'POST':
        team.delete()
        return redirect('team_list')

    return render(request, 'team_confirm_delete.html', {
        'header_matches': header_matches,
        'team': team,
    })


# ============================================================
#                           ИГРОКИ
# ============================================================

def player_list(request):
    header_matches = get_header_matches()

    search = request.GET.get('search', '').strip()
    position = request.GET.get('position', '').strip()
    team_id = request.GET.get('team', '').strip()

    min_goals = request.GET.get('min_goals', '').strip()
    min_assists = request.GET.get('min_assists', '').strip()
    min_yellow = request.GET.get('min_yellow', '').strip()
    min_red = request.GET.get('min_red', '').strip()
    with_team = request.GET.get('with_team', '').strip()

    sort = request.GET.get('sort', 'last_name')
    order = request.GET.get('order', 'asc')

    players = Player.objects.all()

    if search:
        players = players.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)
        )

    if position:
        players = players.filter(position=position)

    if team_id:
        players = players.filter(team_players__team_id=team_id)

    scoring_types = ['гол', 'пенальти_гол']
    players = (
        players
        .annotate(
            goals=Count('events', filter=Q(events__event_type__in=scoring_types)),
            assists=Count('events', filter=Q(events__event_type='ассист')),
            yellow_cards=Count('events', filter=Q(events__event_type='желтая')),
            red_cards=Count('events', filter=Q(events__event_type='красная')),
            matches=Count('events__match', distinct=True),
            main_team_name=Min('team_players__team__name'),
        )
        .prefetch_related('team_players__team')
    )

    def int_or_none(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    g = int_or_none(min_goals)
    if g is not None:
        players = players.filter(goals__gte=g)

    a = int_or_none(min_assists)
    if a is not None:
        players = players.filter(assists__gte=a)

    y = int_or_none(min_yellow)
    if y is not None:
        players = players.filter(yellow_cards__gte=y)

    r = int_or_none(min_red)
    if r is not None:
        players = players.filter(red_cards__gte=r)

    if with_team == '1':
        players = players.filter(team_players__isnull=False)

    sort_map = {
        'last_name': 'last_name',
        'first_name': 'first_name',
        'position': 'position',
        'team': 'main_team_name',
        'goals': 'goals',
        'assists': 'assists',
        'yellow': 'yellow_cards',
        'red': 'red_cards',
        'matches': 'matches',
    }

    sort_key = sort_map.get(sort, 'last_name')

    if order == 'desc':
        sort_expr = '-' + sort_key
    else:
        sort_expr = sort_key

    players = players.order_by(sort_expr, 'last_name', 'first_name')

    context = {
        'header_matches': header_matches,
        'players': players,
        'search': search,
        'position': position,
        'team_id': team_id,
        'sort': sort,
        'order': order,
        'min_goals': min_goals,
        'min_assists': min_assists,
        'min_yellow': min_yellow,
        'min_red': min_red,
        'with_team': with_team,
        'positions': Player.POSITION_CHOICES,
        'teams_filter': Team.objects.order_by('name'),
    }
    return render(request, 'player_list.html', context)


def player_detail(request, player_id):
    header_matches = get_header_matches()
    player = get_object_or_404(Player, pk=player_id)

    tp = TeamPlayer.objects.filter(player=player).select_related('team').first()
    current_team = tp.team if tp else None
    current_number = tp.number if tp else None

    scoring_types = ['гол', 'пенальти_гол']
    assist_type = 'ассист'

    total_goals = MatchEvent.objects.filter(
        player=player,
        event_type__in=scoring_types,
    ).count()

    total_assists = MatchEvent.objects.filter(
        player=player,
        event_type=assist_type,
    ).count()

    total_yellow = MatchEvent.objects.filter(
        player=player,
        event_type='желтая',
    ).count()

    total_red = MatchEvent.objects.filter(
        player=player,
        event_type='красная',
    ).count()

    events = (
        MatchEvent.objects
        .filter(player=player)
        .select_related('match', 'team')
        .order_by('match__date', 'minute', 'added_time')
    )

    per_match = {}
    for e in events:
        key = e.match_id
        if key not in per_match:
            per_match[key] = {
                'match': e.match,
                'team': e.team,
                'goals': 0,
                'assists': 0,
                'yellow': 0,
                'red': 0,
                'is_starting': False,
            }
        if e.event_type in scoring_types:
            per_match[key]['goals'] += 1
        elif e.event_type == assist_type:
            per_match[key]['assists'] += 1
        elif e.event_type == 'желтая':
            per_match[key]['yellow'] += 1
        elif e.event_type == 'красная':
            per_match[key]['red'] += 1

    if per_match:
        lineups = (
            MatchLineup.objects
            .filter(match_id__in=per_match.keys(), player=player)
        )
        for lu in lineups:
            if lu.match_id in per_match and lu.is_starting:
                per_match[lu.match_id]['is_starting'] = True

    matches_stats = list(per_match.values())
    matches_stats.sort(key=lambda r: r['match'].date)

    total_matches = len(matches_stats)
    starts_count = sum(1 for r in matches_stats if r['is_starting'])

    context = {
        'header_matches': header_matches,
        'player': player,
        'current_team': current_team,
        'current_number': current_number,
        'total_goals': total_goals,
        'total_assists': total_assists,
        'total_yellow': total_yellow,
        'total_red': total_red,
        'total_matches': total_matches,
        'starts_count': starts_count,
        'matches_stats': matches_stats,
    }
    return render(request, 'player_detail.html', context)


def player_create(request):
    header_matches = get_header_matches()

    if request.method == 'POST':
        form = PlayerForm(request.POST)
        if form.is_valid():
            player = form.save()
            team_id = request.POST.get('team_id')
            if team_id:
                team = Team.objects.filter(pk=team_id).first()
                if team and not TeamPlayer.objects.filter(player=player).exists():
                    TeamPlayer.objects.create(team=team, player=player)
            return redirect('player_detail', player_id=player.id)
    else:
        form = PlayerForm()

    context = {
        'header_matches': header_matches,
        'form': form,
        'teams': Team.objects.order_by('name'),
    }
    return render(request, 'player_create.html', context)


def player_edit(request, player_id):
    header_matches = get_header_matches()
    player = get_object_or_404(Player, pk=player_id)

    tp = TeamPlayer.objects.filter(player=player).select_related('team').first()
    current_team = tp.team if tp else None

    if request.method == 'POST':

        if 'save_player' in request.POST:
            form = PlayerForm(request.POST, instance=player)

            if form.is_valid():
                player = form.save()
                return redirect('player_detail', player_id=player.id)

        elif 'save_team' in request.POST:
            new_team_id = request.POST.get('new_team_id')

            TeamPlayer.objects.filter(player=player).delete()

            if new_team_id:
                TeamPlayer.objects.create(
                    player=player,
                    team_id=new_team_id
                )

            return redirect('player_edit', player_id=player.id)

    else:
        form = PlayerForm(instance=player)

    tp = TeamPlayer.objects.filter(player=player).select_related('team').first()
    current_team = tp.team if tp else None
    available_teams = Team.objects.order_by('name')

    return render(request, 'player_edit.html', {
        'header_matches': header_matches,
        'player': player,
        'form': form,
        'current_team': current_team,
        'available_teams': available_teams,
    })


def player_delete(request, player_id):
    header_matches = get_header_matches()
    player = get_object_or_404(Player, pk=player_id)

    if request.method == 'POST':
        player.delete()
        return redirect('player_list')

    return render(request, 'player_confirm_delete.html', {
        'header_matches': header_matches,
        'player': player,
    })


# ============================================================
#                           МАТЧИ
# ============================================================

def match_list(request):
    header_matches = get_header_matches()

    matches_qs = (
        Match.objects
        .select_related('home_team', 'away_team')
        .order_by('-date')
    )

    matches = list(matches_qs)

    for m in matches:
        if m.status in ['завершён', 'идёт']:
            m.home_goals, m.away_goals = get_match_score(m)
        else:
            m.home_goals = None
            m.away_goals = None

    return render(request, 'match_list.html', {
        'header_matches': header_matches,
        'matches': matches,
    })


def match_detail(request, match_id):
    header_matches = get_header_matches()
    match = get_object_or_404(Match, pk=match_id)

    scoring_types = ['гол', 'пенальти_гол']
    own_goal_type = 'автогол'

    # --- СЧЁТ С УЧЁТОМ АВТОГОЛОВ ---
    home_goals, away_goals = get_match_score(match)

    events = (
        MatchEvent.objects
        .filter(match=match)
        .select_related('team', 'player')
        .order_by('minute', 'added_time', 'id')
    )

    home_lineups = (
        MatchLineup.objects
        .filter(match=match, team=match.home_team)
        .select_related('player')
    )
    away_lineups = (
        MatchLineup.objects
        .filter(match=match, team=match.away_team)
        .select_related('player')
    )

    lineup_player_ids = [lu.player_id for lu in home_lineups] + [lu.player_id for lu in away_lineups]

    tp_qs = TeamPlayer.objects.filter(
        team__in=[match.home_team, match.away_team],
        player_id__in=lineup_player_ids
    )

    number_map = {}
    for tp in tp_qs:
        number_map[(tp.team_id, tp.player_id)] = tp.number

    stats_map = {}
    for e in events:
        if not e.player_id or not e.team_id:
            continue
        key = (e.player_id, e.team_id)
        if key not in stats_map:
            stats_map[key] = {
                'goals': 0,
                'assists': 0,
                'yellow': 0,
                'red': 0,
            }
        s = stats_map[key]
        if e.event_type in scoring_types:
            s['goals'] += 1
        elif e.event_type == 'ассист':
            s['assists'] += 1
        elif e.event_type == 'желтая':
            s['yellow'] += 1
        elif e.event_type == 'красная':
            s['red'] += 1

    def build_team_events(team):
        team_events = [e for e in events if e.team_id == team.id]
        display = []
        used_ids = set()

        sub_out_ids = set()
        sub_in_ids = set()

        i = 0
        while i < len(team_events):
            e = team_events[i]
            if e.id in used_ids:
                i += 1
                continue

            et = e.event_type

            if et in ['гол', 'пенальти_гол']:
                assist_ev = None
                for j in range(i + 1, len(team_events)):
                    ae = team_events[j]
                    if ae.id in used_ids:
                        continue
                    if (ae.event_type == 'ассист' and
                            ae.minute == e.minute and
                            ae.added_time == e.added_time):
                        assist_ev = ae
                        used_ids.add(ae.id)
                        break

                display.append({
                    'kind': 'goal',
                    'minute': e.minute,
                    'added': e.added_time,
                    'player': e.player,
                    'is_penalty': (et == 'пенальти_гол'),
                    'assist': assist_ev.player if assist_ev and assist_ev.player_id else None,
                })
                used_ids.add(e.id)
                i += 1
                continue

            if et == 'автогол':
                display.append({
                    'kind': 'own_goal',
                    'minute': e.minute,
                    'added': e.added_time,
                    'player': e.player,
                })
                used_ids.add(e.id)
                i += 1
                continue

            if et in ['желтая', 'красная']:
                display.append({
                    'kind': 'card',
                    'minute': e.minute,
                    'added': e.added_time,
                    'player': e.player,
                    'card': 'yellow' if et == 'желтая' else 'red',
                })
                used_ids.add(e.id)
                i += 1
                continue

            if et == 'замена':
                out_ev = e
                in_ev = None
                if i + 1 < len(team_events):
                    cand = team_events[i + 1]
                    if (cand.event_type == 'замена' and
                            cand.minute == e.minute and
                            cand.added_time == e.added_time):
                        in_ev = cand
                        used_ids.add(cand.id)
                        i += 1

                display.append({
                    'kind': 'sub',
                    'minute': e.minute,
                    'added': e.added_time,
                    'player_out': out_ev.player,
                    'player_in': in_ev.player if in_ev else None,
                })

                if out_ev.player_id:
                    sub_out_ids.add(out_ev.player_id)
                if in_ev and in_ev.player_id:
                    sub_in_ids.add(in_ev.player_id)

                used_ids.add(out_ev.id)
                i += 1
                continue

            i += 1

        return display, sub_out_ids, sub_in_ids

    home_events_display, home_sub_out_ids, home_sub_in_ids = build_team_events(match.home_team)
    away_events_display, away_sub_out_ids, away_sub_in_ids = build_team_events(match.away_team)

    def position_rank(player):
        code = (player.position or '').upper()
        if code in ('ВРТ'):
            return 1
        if code in ('ЗАЩ'):
            return 2
        if code in ('ПЗ'):
            return 3
        if code in ('НАП'):
            return 4
        return 5

    def build_squad(lineups, team, sub_out_ids, sub_in_ids):
        squad = []
        for lu in lineups:
            key = (lu.player_id, lu.team_id)
            st = stats_map.get(key, {'goals': 0, 'assists': 0, 'yellow': 0, 'red': 0})
            num = number_map.get(key)
            player = lu.player
            squad.append({
                'player': player,
                'number': num,
                'goals': st['goals'],
                'assists': st['assists'],
                'yellow': st['yellow'],
                'red': st['red'],
                'is_starting': lu.is_starting,
                'pos_rank': position_rank(player),
                'was_subbed_off': player.id in sub_out_ids,
                'came_from_bench': player.id in sub_in_ids,
            })

        squad.sort(
            key=lambda r: (
                r['pos_rank'],
                r['player'].last_name,
                r['player'].first_name,
            )
        )

        starters = [r for r in squad if r['is_starting']]
        bench = [r for r in squad if not r['is_starting']]

        return starters, bench

    home_starters, home_bench = build_squad(
        home_lineups, match.home_team,
        home_sub_out_ids, home_sub_in_ids
    )
    away_starters, away_bench = build_squad(
        away_lineups, match.away_team,
        away_sub_out_ids, away_sub_in_ids
    )

    context = {
        'header_matches': header_matches,
        'match': match,
        'home_goals': home_goals,
        'away_goals': away_goals,
        'home_events_display': home_events_display,
        'away_events_display': away_events_display,
        'home_starters': home_starters,
        'home_bench': home_bench,
        'away_starters': away_starters,
        'away_bench': away_bench,
    }
    return render(request, 'match_detail.html', context)


def match_create(request):
    """
    Создание матча на одной странице:
    - выбор команд, даты, статуса (card UI)
    - сразу выбор заявки и старта для обеих команд
    """
    header_matches = get_header_matches()

    all_team_players_qs = (
        TeamPlayer.objects
        .select_related('player', 'team')
    )

    all_team_players = sort_team_players_by_position(all_team_players_qs)

    if request.method == 'POST':
        form = MatchForm(request.POST)
        if form.is_valid():
            match = form.save()

            def to_ids(name):
                ids = set()
                for val in request.POST.getlist(name):
                    try:
                        ids.add(int(val))
                    except ValueError:
                        pass
                return ids

            home_ids = to_ids('home_players')
            away_ids = to_ids('away_players')
            home_start_ids = to_ids('home_starters')
            away_start_ids = to_ids('away_starters')

            home_team = form.cleaned_data['home_team']
            away_team = form.cleaned_data['away_team']

            def limit_starters(selected_ids, team):
                result = set()
                for tp in all_team_players:
                    if tp.team_id != team.id:
                        continue
                    if tp.player_id in selected_ids:
                        result.add(tp.player_id)
                        if len(result) >= 11:
                            break
                return result

            home_start_ids = limit_starters(home_start_ids, home_team)
            away_start_ids = limit_starters(away_start_ids, away_team)

            selected_ids = home_ids | away_ids
            for pid in selected_ids:

                if pid in home_ids:
                    team_for_player = home_team
                elif pid in away_ids:
                    team_for_player = away_team
                else:
                    continue

                is_starting = (
                    (team_for_player == home_team and pid in home_start_ids) or
                    (team_for_player == away_team and pid in away_start_ids)
                )

                MatchLineup.objects.create(
                    match=match,
                    team=team_for_player,
                    player_id=pid,
                    is_starting=is_starting,
                )

            return redirect('match_detail', match_id=match.id)

    else:
        form = MatchForm()

    return render(request, 'match_create.html', {
        'header_matches': header_matches,
        'form': form,
        'all_team_players': all_team_players,
    })


def match_edit(request, match_id):
    """
    Редактирование матча:
    - общие данные (команды, дата, статус pills)
    - составы (заявка/старт), старт <= 11
    """
    header_matches = get_header_matches()
    match = get_object_or_404(Match, pk=match_id)

    home_team_players_qs = (
        TeamPlayer.objects
        .filter(team=match.home_team)
        .select_related('player', 'team')
    )
    away_team_players_qs = (
        TeamPlayer.objects
        .filter(team=match.away_team)
        .select_related('player', 'team')
    )

    home_team_players = sort_team_players_by_position(home_team_players_qs)
    away_team_players = sort_team_players_by_position(away_team_players_qs)

    player_team_map = {}
    for tp in home_team_players:
        player_team_map[tp.player_id] = match.home_team
    for tp in away_team_players:
        player_team_map[tp.player_id] = match.away_team

    player_position_map = {}
    for tp in home_team_players:
        player_position_map[tp.player_id] = tp.player.position
    for tp in away_team_players:
        player_position_map[tp.player_id] = tp.player.position

    if request.method == 'POST':

        if 'save_match' in request.POST:
            form = MatchForm(request.POST, instance=match)
            if form.is_valid():
                form.save()
                return redirect('match_edit', match_id=match.id)

        elif 'save_lineups' in request.POST:
            form = MatchForm(instance=match)

            def to_int_set(name):
                out = set()
                for v in request.POST.getlist(name):
                    try:
                        out.add(int(v))
                    except ValueError:
                        pass
                return out

            home_ids = to_int_set('home_players')
            away_ids = to_int_set('away_players')
            home_start_ids = to_int_set('home_starters')
            away_start_ids = to_int_set('away_starters')

            valid_home_starters = set()
            for tp in home_team_players:
                if tp.player_id in home_start_ids:
                    valid_home_starters.add(tp.player_id)
                    if len(valid_home_starters) >= 11:
                        break
            home_start_ids = valid_home_starters

            valid_away_starters = set()
            for tp in away_team_players:
                if tp.player_id in away_start_ids:
                    valid_away_starters.add(tp.player_id)
                    if len(valid_away_starters) >= 11:
                        break
            away_start_ids = valid_away_starters

            selected_ids = home_ids | away_ids
            starter_ids = home_start_ids | away_start_ids

            existing_lineups = list(MatchLineup.objects.filter(match=match))
            existing_by_player = {lu.player_id: lu for lu in existing_lineups}

            for lu in existing_lineups:
                if lu.player_id not in selected_ids:
                    lu.delete()

            for pid in selected_ids:
                team_for_player = player_team_map.get(pid)
                if not team_for_player:
                    continue

                is_starting = pid in starter_ids
                pos_value = player_position_map.get(pid) or ''

                if pid in existing_by_player:
                    lu = existing_by_player[pid]
                    lu.team = team_for_player
                    lu.is_starting = is_starting
                    lu.save()
                else:
                    # ✅ вместо raw SQL — ORM, логика та же
                    MatchLineup.objects.create(
                        match=match,
                        team=team_for_player,
                        player_id=pid,
                        is_starting=is_starting,
                        position=pos_value  # если поле есть — сохранит, если blank=True — ок
                    )

            return redirect('match_edit', match_id=match.id)

    form = MatchForm(instance=match)

    lineups = list(MatchLineup.objects.filter(match=match))
    lineup_player_ids = {lu.player_id for lu in lineups}
    starting_player_ids = {lu.player_id for lu in lineups if lu.is_starting}

    context = {
        'header_matches': header_matches,
        'match': match,
        'form': form,
        'home_team_players': home_team_players,
        'away_team_players': away_team_players,
        'lineup_player_ids': lineup_player_ids,
        'starting_player_ids': starting_player_ids,
    }
    return render(request, 'match_edit.html', context)


def match_events_edit(request, match_id):
    """
    Отдельная страница для управления событиями матча.
    (логика без изменений)
    """
    header_matches = get_header_matches()
    match = get_object_or_404(Match, pk=match_id)

    home_lineups = (
        MatchLineup.objects
        .filter(match=match, team=match.home_team)
        .select_related('player')
        .order_by('player__last_name', 'player__first_name')
    )
    away_lineups = (
        MatchLineup.objects
        .filter(match=match, team=match.away_team)
        .select_related('player')
        .order_by('player__last_name', 'player__first_name')
    )

    home_bench_lineups = [lu for lu in home_lineups if not lu.is_starting]
    away_bench_lineups = [lu for lu in away_lineups if not lu.is_starting]

    home_squad_ids = {lu.player_id for lu in home_lineups}
    away_squad_ids = {lu.player_id for lu in away_lineups}

    events = (
        MatchEvent.objects
        .filter(match=match)
        .select_related('team', 'player')
        .order_by('minute', 'added_time', 'id')
    )

    errors = []

    def get_on_field_player_ids(team, minute, added_time):
        lineups = MatchLineup.objects.filter(match=match, team=team)
        starting = {lu.player_id for lu in lineups if lu.is_starting}
        on_field = set(starting)

        subs = (
            MatchEvent.objects
            .filter(match=match, team=team, event_type='замена')
            .order_by('minute', 'added_time', 'id')
        )

        target = (minute or 0, added_time or 0)

        def time_key(e):
            return (e.minute or 0, e.added_time or 0)

        for (m, a), group in groupby(subs, key=time_key):
            if (m, a) >= target:
                break
            group = list(group)
            players_in_group = [e.player_id for e in group if e.player_id]

            out_candidates = [pid for pid in players_in_group if pid in on_field]
            in_candidates = [pid for pid in players_in_group if pid not in on_field]

            for pid in out_candidates:
                on_field.discard(pid)
            for pid in in_candidates:
                on_field.add(pid)

        return on_field

    if request.method == 'POST':
        if 'delete_events' in request.POST:
            ids = request.POST.getlist('event_id')
            if ids:
                base_qs = MatchEvent.objects.filter(match=match, id__in=ids)
                to_delete = set(base_qs.values_list('id', flat=True))

                for ev in base_qs:
                    if ev.team_id is None:
                        team_filter = {'team__isnull': True}
                    else:
                        team_filter = {'team': ev.team}

                    if ev.event_type in ['гол', 'пенальти_гол']:
                        assist_ids = MatchEvent.objects.filter(
                            match=match,
                            event_type='ассист',
                            minute=ev.minute,
                            added_time=ev.added_time,
                            **team_filter
                        ).values_list('id', flat=True)
                        to_delete.update(assist_ids)

                    if ev.event_type == 'ассист':
                        goal_ids = MatchEvent.objects.filter(
                            match=match,
                            event_type__in=['гол', 'пенальти_гол'],
                            minute=ev.minute,
                            added_time=ev.added_time,
                            **team_filter
                        ).values_list('id', flat=True)
                        to_delete.update(goal_ids)

                    if ev.event_type == 'замена':
                        sub_ids = MatchEvent.objects.filter(
                            match=match,
                            event_type='замена',
                            minute=ev.minute,
                            added_time=ev.added_time,
                            **team_filter
                        ).values_list('id', flat=True)
                        to_delete.update(sub_ids)

                MatchEvent.objects.filter(match=match, id__in=to_delete).delete()

            return redirect('match_events_edit', match_id=match.id)

        elif 'add_event' in request.POST:
            event_team_id = request.POST.get('event_team')
            mode = request.POST.get('event_mode')
            minute_val = request.POST.get('event_minute')
            added_val = request.POST.get('event_added_time', '').strip()

            if not event_team_id:
                errors.append("Выберите команду.")

            try:
                minute = int(minute_val)
            except (TypeError, ValueError):
                minute = None

            if minute is None or minute < 1 or minute > 90:
                errors.append("Минута события должна быть в диапазоне от 1 до 90.")

            if added_val == '':
                added_time = None
            else:
                try:
                    added_time = int(added_val)
                except ValueError:
                    added_time = None

            if not errors:
                team = Team.objects.filter(pk=event_team_id).first()
                if not team:
                    errors.append("Команда не найдена.")
                else:
                    if team == match.home_team:
                        squad_ids_this = home_squad_ids
                    else:
                        squad_ids_this = away_squad_ids

                    if mode == 'goal':
                        scorer_id = request.POST.get('event_player')
                        assist_id = request.POST.get('assist_player') or None

                        if not scorer_id:
                            errors.append("Выберите автора гола.")
                        else:
                            try:
                                scorer_id = int(scorer_id)
                            except ValueError:
                                scorer_id = None
                                errors.append("Некорректный игрок для гола.")

                        if assist_id:
                            try:
                                assist_id = int(assist_id)
                            except ValueError:
                                assist_id = None
                                errors.append("Некорректный ассистент.")

                        if scorer_id and scorer_id not in squad_ids_this:
                            errors.append("Автор гола должен быть в заявке выбранной команды.")
                        if assist_id and assist_id not in squad_ids_this:
                            errors.append("Ассистент должен быть в заявке выбранной команды.")

                        on_field = get_on_field_player_ids(team, minute, added_time)

                        if scorer_id and scorer_id not in on_field:
                            errors.append("Автор гола должен находиться на поле в момент гола.")
                        if assist_id and assist_id not in on_field:
                            errors.append("Ассистент должен находиться на поле в момент гола.")

                        if assist_id and scorer_id and assist_id == scorer_id:
                            errors.append("Ассистент и автор гола не могут быть одним и тем же.")

                        if not errors:
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=scorer_id,
                                event_type='гол',
                                minute=minute,
                                added_time=added_time,
                            )
                            if assist_id:
                                MatchEvent.objects.create(
                                    match=match,
                                    team=team,
                                    player_id=assist_id,
                                    event_type='ассист',
                                    minute=minute,
                                    added_time=added_time,
                                )
                            return redirect('match_events_edit', match_id=match.id)

                    elif mode == 'penalty':
                        scorer_id = request.POST.get('event_player')

                        if not scorer_id:
                            errors.append("Выберите исполнителя пенальти.")
                        else:
                            try:
                                scorer_id = int(scorer_id)
                            except ValueError:
                                scorer_id = None
                                errors.append("Некорректный игрок для пенальти.")

                        if scorer_id and scorer_id not in squad_ids_this:
                            errors.append("Игрок должен быть в заявке выбранной команды.")

                        on_field = get_on_field_player_ids(team, minute, added_time)

                        if scorer_id and scorer_id not in on_field:
                            errors.append("Исполнитель пенальти должен быть на поле в момент удара.")

                        if not errors:
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=scorer_id,
                                event_type='пенальти_гол',
                                minute=minute,
                                added_time=added_time,
                            )
                            return redirect('match_events_edit', match_id=match.id)

                    elif mode == 'own':
                        player_id = request.POST.get('event_player')

                        if not player_id:
                            errors.append("Выберите игрока, который забил автогол.")
                        else:
                            try:
                                player_id = int(player_id)
                            except ValueError:
                                player_id = None
                                errors.append("Некорректный игрок для автогола.")

                        if player_id and player_id not in squad_ids_this:
                            errors.append("Игрок автогола должен быть в заявке выбранной команды.")

                        on_field_actual = get_on_field_player_ids(team, minute, added_time)
                        if player_id and player_id not in on_field_actual:
                            errors.append("Игрок, забивший автогол, должен быть на поле в этот момент.")

                        if not errors:
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=player_id,
                                event_type='автогол',
                                minute=minute,
                                added_time=added_time,
                            )
                            return redirect('match_events_edit', match_id=match.id)

                    elif mode in ['yellow', 'red']:
                        player_id = request.POST.get('event_player')
                        if not player_id:
                            errors.append("Выберите игрока для карточки.")
                        else:
                            try:
                                player_id = int(player_id)
                            except ValueError:
                                player_id = None
                                errors.append("Некорректный игрок для карточки.")

                        if player_id and player_id not in squad_ids_this:
                            errors.append("Игрок должен быть в заявке выбранной команды.")

                        on_field = get_on_field_player_ids(team, minute, added_time)

                        if player_id and player_id not in on_field:
                            errors.append("Карточку может получить только игрок, находящийся на поле.")

                        if not errors:
                            event_type = 'желтая' if mode == 'yellow' else 'красная'
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=player_id,
                                event_type=event_type,
                                minute=minute,
                                added_time=added_time,
                            )
                            return redirect('match_events_edit', match_id=match.id)

                    elif mode == 'sub':
                        player_out_id = request.POST.get('sub_out')
                        player_in_id = request.POST.get('sub_in')

                        try:
                            player_out_id = int(player_out_id) if player_out_id else None
                        except ValueError:
                            player_out_id = None
                        try:
                            player_in_id = int(player_in_id) if player_in_id else None
                        except ValueError:
                            player_in_id = None

                        if not player_out_id or not player_in_id:
                            errors.append("Выберите обоих игроков для замены (кто уходит и кто выходит).")

                        if player_out_id and player_out_id not in squad_ids_this:
                            errors.append("Игрок, который уходит, должен быть в заявке выбранной команды.")
                        if player_in_id and player_in_id not in squad_ids_this:
                            errors.append("Игрок, который выходит, должен быть в заявке выбранной команды.")

                        on_field = get_on_field_player_ids(team, minute, added_time)

                        if player_out_id and player_out_id not in on_field:
                            errors.append("Игрок, который уходит, должен находиться на поле в момент замены.")

                        if player_in_id and player_in_id in on_field:
                            errors.append("Игрок, который выходит, не должен уже находиться на поле.")

                        if player_in_id and player_out_id and player_in_id == player_out_id:
                            errors.append("Нельзя заменить игрока самим собой.")

                        if not errors:
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=player_out_id,
                                event_type='замена',
                                minute=minute,
                                added_time=added_time,
                            )
                            MatchEvent.objects.create(
                                match=match,
                                team=team,
                                player_id=player_in_id,
                                event_type='замена',
                                minute=minute,
                                added_time=added_time,
                            )
                            return redirect('match_events_edit', match_id=match.id)

                    else:
                        errors.append("Выберите тип события.")

    context = {
        'header_matches': header_matches,
        'match': match,
        'events': events,
        'home_lineups': home_lineups,
        'away_lineups': away_lineups,
        'home_bench_lineups': home_bench_lineups,
        'away_bench_lineups': away_bench_lineups,
        'errors': errors,
    }
    return render(request, 'match_events_edit.html', context)


def match_delete(request, match_id):
    header_matches = get_header_matches()
    match = get_object_or_404(Match, pk=match_id)

    if request.method == 'POST':
        match.delete()
        return redirect('match_list')

    return render(request, 'match_confirm_delete.html', {
        'header_matches': header_matches,
        'match': match,
    })


# ============================================================
#                   СТАТИСТИКА / ТАБЛИЦА / ОТЧЁТЫ
# ============================================================

def stats_view(request):
    header_matches = get_header_matches()

    tab = request.GET.get('tab', 'overview')
    team_param = request.GET.get('team', '').strip()
    position_param = request.GET.get('position', '').strip()

    scoring_types = ['гол', 'пенальти_гол']

    players = Player.objects.all()

    if position_param:
        players = players.filter(position=position_param)

    team_filter = None
    if team_param.isdigit():
        team_filter = int(team_param)
        players = players.filter(team_players__team_id=team_filter)

    players = (
        players
        .annotate(
            goals=Count(
                'events',
                filter=Q(events__event_type__in=scoring_types),
                distinct=True
            ),
            assists=Count(
                'events',
                filter=Q(events__event_type='ассист'),
                distinct=True
            ),
            yellow_cards=Count(
                'events',
                filter=Q(events__event_type='желтая'),
                distinct=True
            ),
            red_cards=Count(
                'events',
                filter=Q(events__event_type='красная'),
                distinct=True
            ),
            games=Count('lineups__match', distinct=True),
        )
        .prefetch_related('team_players__team')
        .order_by('last_name', 'first_name')
    )

    teams = Team.objects.order_by('name')
    positions = Player.POSITION_CHOICES

    def get_top(metric, limit=5):
        return (
            players
            .filter(**{f"{metric}__gt": 0})
            .order_by(f"-{metric}", "games", "last_name", "first_name")[:limit]
        )

    top_scorers = []
    top_assistants = []
    top_yellow = []
    top_red = []

    metric = None
    metric_label = ''
    players_list = []

    if tab == 'overview':
        top_scorers = get_top('goals', 5)
        top_assistants = get_top('assists', 5)
        top_yellow = get_top('yellow_cards', 5)
        top_red = get_top('red_cards', 5)

    elif tab == 'goals':
        metric = 'goals'
        metric_label = 'Голы'
        players_list = (
            players
            .filter(goals__gt=0)
            .order_by('-goals', 'games', 'last_name', 'first_name')[:10]
        )

    elif tab == 'assists':
        metric = 'assists'
        metric_label = 'Голевые передачи'
        players_list = (
            players
            .filter(assists__gt=0)
            .order_by('-assists', 'games', 'last_name', 'first_name')[:10]
        )

    elif tab == 'yellow':
        metric = 'yellow_cards'
        metric_label = 'Жёлтые карточки'
        players_list = (
            players
            .filter(yellow_cards__gt=0)
            .order_by('-yellow_cards', 'games', 'last_name', 'first_name')[:10]
        )

    elif tab == 'red':
        metric = 'red_cards'
        metric_label = 'Красные карточки'
        players_list = (
            players
            .filter(red_cards__gt=0)
            .order_by('-red_cards', 'games', 'last_name', 'first_name')[:10]
        )

    else:
        tab = 'overview'
        top_scorers = get_top('goals', 5)
        top_assistants = get_top('assists', 5)
        top_yellow = get_top('yellow_cards', 5)
        top_red = get_top('red_cards', 5)

    context = {
        'header_matches': header_matches,
        'tab': tab,

        'team_id': team_param,
        'position': position_param,
        'teams': teams,
        'positions': positions,

        'top_scorers': top_scorers,
        'top_assistants': top_assistants,
        'top_yellow': top_yellow,
        'top_red': top_red,

        'metric': metric,
        'metric_label': metric_label,
        'players_list': players_list,
    }
    return render(request, 'stats.html', context)


def table_view(request):
    header_matches = get_header_matches()
    standings = _calculate_standings()
    return render(request, 'table.html', {
        'header_matches': header_matches,
        'standings': standings,
    })


def reports_view(request):
    header_matches = get_header_matches()

    kind = request.GET.get('kind', '')
    fmt = request.GET.get('format', '')
    team_id = request.GET.get('team') or ''
    position = request.GET.get('position') or ''

    match_team_id = request.GET.get('match_team') or ''
    match_status = request.GET.get('match_status') or ''

    download = request.GET.get('download')

    teams = Team.objects.order_by('name')
    positions = Player.POSITION_CHOICES

    if not download or kind not in ['players', 'teams', 'matches'] or fmt not in ['excel', 'txt']:
        return render(request, 'reports.html', {
            'header_matches': header_matches,
            'teams': teams,
            'positions': positions,
            'kind': kind,
            'fmt': fmt,
            'team_id': team_id,
            'position': position,
            'match_team_id': match_team_id,
            'match_status': match_status,
        })

    if kind == 'players':
        players = Player.objects.all()

        if team_id:
            players = players.filter(team_players__team_id=team_id)

        if position:
            players = players.filter(position=position)

        scoring_types = ['гол', 'пенальти_гол']
        players = (
            players
            .annotate(
                goals=Count('events', filter=Q(events__event_type__in=scoring_types)),
                assists=Count('events', filter=Q(events__event_type='ассист')),
                yellow_cards=Count('events', filter=Q(events__event_type='желтая')),
                red_cards=Count('events', filter=Q(events__event_type='красная')),
            )
            .order_by('last_name', 'first_name')
        )

        tps = (
            TeamPlayer.objects
            .filter(player__in=players)
            .select_related('team')
        )
        team_by_player = {}
        for tp in tps:
            team_by_player.setdefault(tp.player_id, tp.team)

        matches_qs = (
            MatchEvent.objects
            .filter(player__in=players)
            .values('player_id')
            .annotate(match_count=Count('match', distinct=True))
        )
        matches_map = {row['player_id']: row['match_count'] for row in matches_qs}

        buffer = io.StringIO()
        buffer.write("Игрок\tКоманда\tПозиция\tМатчи\tГолы\tАссисты\tЖК\tКК\n")

        for p in players:
            team = team_by_player.get(p.id)
            team_name = team.name if team else ''

            pos_display = p.position or ''
            games = matches_map.get(p.id, 0)

            line = f"{p.last_name} {p.first_name}\t{team_name}\t{pos_display}\t{games}\t{p.goals}\t{p.assists}\t{p.yellow_cards}\t{p.red_cards}\n"
            buffer.write(line)

        content = buffer.getvalue()
        buffer.close()

        if fmt == 'excel':
            data = content.encode('cp1251', errors='replace')
            response = HttpResponse(
                data,
                content_type='application/vnd.ms-excel; charset=windows-1251'
            )
            response['Content-Disposition'] = 'attachment; filename="players_report.xls"'
            return response
        else:
            response = HttpResponse(content, content_type='text/plain; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="players_report.txt"'
            return response

    if kind == 'teams':
        standings = _calculate_standings()

        buffer = io.StringIO()
        buffer.write("Команда\tГород\tИ\tВ\tН\tП\tЗабито\tПропущено\tРазница\tОчки\n")

        for row in standings:
            team = row['team']
            diff = row['goals_for'] - row['goals_against']
            city = team.city or ''
            line = (
                f"{team.name}\t"
                f"{city}\t"
                f"{row['games']}\t{row['wins']}\t{row['draws']}\t{row['losses']}\t"
                f"{row['goals_for']}\t{row['goals_against']}\t{diff}\t{row['points']}\n"
            )
            buffer.write(line)

        content = buffer.getvalue()
        buffer.close()

        if fmt == 'excel':
            data = content.encode('cp1251', errors='replace')
            response = HttpResponse(
                data,
                content_type='application/vnd.ms-excel; charset=windows-1251'
            )
            response['Content-Disposition'] = 'attachment; filename="teams_table.xls"'
            return response
        else:
            response = HttpResponse(content, content_type='text/plain; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="teams_table.txt"'
            return response

    if kind == 'matches':
        matches = (
            Match.objects
            .select_related('home_team', 'away_team')
        )

        if match_team_id:
            matches = matches.filter(
                Q(home_team_id=match_team_id) | Q(away_team_id=match_team_id)
            )

        if match_status:
            matches = matches.filter(status=match_status)

        matches = matches.order_by('date')

        buffer = io.StringIO()
        buffer.write("Дата/время\tХозяева\tГости\tСчёт\tСтатус\n")

        for m in matches:
            if m.status in ['завершён', 'идёт']:
                hg, ag = get_match_score(m)
                score_str = f"{hg}:{ag}"
            else:
                score_str = "-:-"

            date_str = m.date.strftime('%d.%m.%Y %H:%M')
            line = f"{date_str}\t{m.home_team.name}\t{m.away_team.name}\t{score_str}\t{m.get_status_display()}\n"
            buffer.write(line)

        content = buffer.getvalue()
        buffer.close()

        if fmt == 'excel':
            data = content.encode('cp1251', errors='replace')
            response = HttpResponse(
                data,
                content_type='application/vnd.ms-excel; charset=windows-1251'
            )
            response['Content-Disposition'] = 'attachment; filename="matches_report.xls"'
            return response
        else:
            response = HttpResponse(content, content_type='text/plain; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="matches_report.txt"'
            return response

    return render(request, 'reports.html', {
        'header_matches': header_matches,
        'teams': teams,
        'positions': positions,
        'kind': kind,
        'fmt': fmt,
        'team_id': team_id,
        'position': position,
        'match_team_id': match_team_id,
        'match_status': match_status,
    })
