from django.db import models


# ТРЕНЕРЫ  (таблица: `тренеры`)
class Coach(models.Model):
    first_name = models.CharField(max_length=50, db_column='имя')
    last_name = models.CharField(max_length=50, db_column='фамилия')
    birth_date = models.DateField(null=True, blank=True, db_column='дата_рождения')

    class Meta:
        db_table = 'тренеры'

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


# КОМАНДЫ  (таблица: `команды`)
class Team(models.Model):
    id = models.AutoField(primary_key=True, db_column='id_команды')
    name = models.CharField(max_length=100, db_column='название')
    city = models.CharField(max_length=50, null=True, blank=True, db_column='город')
    coach = models.ForeignKey(
        Coach,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_column='FK_id_тренера',
        related_name='teams',
    )
    emblem = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_column='эмблема',
        help_text="Путь к файлу эмблемы или URL",
    )

    class Meta:
        db_table = 'команды'

    def __str__(self):
        return self.name


# ИГРОКИ (таблица: `игроки`)
class Player(models.Model):
    POSITION_CHOICES = [
        ('ВРТ', 'Вратарь'),
        ('ЗАЩ', 'Защитник'),
        ('ПЗ',  'Полузащитник'),
        ('НАП', 'Нападающий'),
    ]

    id = models.AutoField(primary_key=True, db_column='id_игрока')
    first_name = models.CharField(max_length=50, db_column='имя')
    last_name = models.CharField(max_length=50, db_column='фамилия')
    birth_date = models.DateField(null=True, blank=True, db_column='дата_рождения')

    position = models.CharField(
        max_length=3,           # ← теперь правильно!
        choices=POSITION_CHOICES,
        db_column='позиция',
    )

    class Meta:
        db_table = 'игроки'

    def __str__(self):
        return f"{self.first_name} {self.last_name}"



# ИГРОКИ_КОМАНД (таблица: `игроки_команд`)
class TeamPlayer(models.Model):
    id = models.AutoField(primary_key=True, db_column='id_состава')
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column='FK_id_команды',
        related_name='team_players',
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        db_column='FK_id_игрока',
        related_name='team_players',
    )
    number = models.IntegerField(null=True, blank=True, db_column='номер')

    class Meta:
        db_table = 'игроки_команд'
        unique_together = (('team', 'player'),)

    def __str__(self):
        if self.number:
            return f"{self.team} – #{self.number} {self.player}"
        return f"{self.team} – {self.player}"


# МАТЧИ (таблица: `матчи`)
class Match(models.Model):
    STATUS_CHOICES = [
        ('запланирован', 'Запланирован'),
        ('идёт', 'Идёт'),
        ('завершён', 'Завершён'),
    ]

    id = models.AutoField(primary_key=True, db_column='id_матча')
    home_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column='FK_id_команды_хозяев',
        related_name='home_matches',
    )
    away_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column='FK_id_команды_гостей',
        related_name='away_matches',
    )
    date = models.DateTimeField(db_column='дата')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='запланирован',
        db_column='статус',
    )

    class Meta:
        db_table = 'матчи'

    def __str__(self):
        return f"{self.home_team} – {self.away_team} ({self.date:%d.%m.%Y})"


# СОСТАВ_НА_МАТЧ (таблица: `состав_на_матч`)
class MatchLineup(models.Model):
    id = models.AutoField(primary_key=True, db_column='id_записи')
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        db_column='FK_id_матча',
        related_name='lineups',
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column='FK_id_команды',
        related_name='lineups',
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        db_column='FK_id_игрока',
        related_name='lineups',
    )
    position = models.CharField(
        max_length=3,
        choices=Player.POSITION_CHOICES,
        db_column='позиция',
        default='ПЗ',  # или без default, но тогда в скрипте всегда передавай
    )
    is_starting = models.BooleanField(default=False, db_column='в_старте')

    class Meta:
        db_table = 'состав_на_матч'
        unique_together = (('match', 'player'),)

    def __str__(self):
        return f"{self.match} – {self.player} ({'старт' if self.is_starting else 'запас'})"


# СОБЫТИЯ_МАТЧА (таблица: `события_матча`)
class MatchEvent(models.Model):
    EVENT_TYPE_CHOICES = [
        ('гол', 'Гол'),
        ('ассист', 'Голевой пас'),
        ('автогол', 'Автогол'),
        ('пенальти_гол', 'Гол с пенальти'),
        ('желтая', 'Жёлтая карточка'),
        ('красная', 'Красная карточка'),
        ('замена', 'Замена'),
    ]

    id = models.AutoField(primary_key=True, db_column='id_события')
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        db_column='FK_id_матча',
        related_name='events',
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        db_column='FK_id_команды',
        related_name='events',
    )
    player = models.ForeignKey(
        Player,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_column='FK_id_игрока',
        related_name='events',
    )
    event_type = models.CharField(
        max_length=20,
        choices=EVENT_TYPE_CHOICES,
        db_column='тип',
    )
    minute = models.IntegerField(db_column='минута')
    added_time = models.IntegerField(null=True, blank=True, db_column='добавленное_время')

    class Meta:
        db_table = 'события_матча'

    def __str__(self):
        if self.added_time:
            t = f"{self.minute}+{self.added_time}'"
        else:
            t = f"{self.minute}'"
        return f"{self.match} – {self.event_type} ({t})"
