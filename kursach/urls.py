# urls.py
from django.contrib import admin
from django.urls import path
from football import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),

    path('teams/', views.team_list, name='team_list'),
    path('teams/add/', views.team_create, name='team_create'),
    path('teams/<int:team_id>/', views.team_detail, name='team_detail'),
    path('teams/<int:team_id>/edit/', views.team_edit, name='team_edit'),
    path('teams/<int:team_id>/squad/', views.team_squad_edit, name='team_squad_edit'),
    path('teams/<int:team_id>/delete/', views.team_delete, name='team_delete'),

    path('players/', views.player_list, name='player_list'),
    path('players/add/', views.player_create, name='player_create'),
    path('players/<int:player_id>/', views.player_detail, name='player_detail'),
    path('players/<int:player_id>/edit/', views.player_edit, name='player_edit'),
    path('players/<int:player_id>/delete/', views.player_delete, name='player_delete'),

    path('matches/add/', views.match_create, name='match_create'),
    path('matches/', views.match_list, name='match_list'),
    path('matches/<int:match_id>/', views.match_detail, name='match_detail'),
    path('matches/<int:match_id>/edit/', views.match_edit, name='match_edit'),
    path('matches/<int:match_id>/events/', views.match_events_edit, name='match_events_edit'),
    path('matches/<int:match_id>/delete/', views.match_delete, name='match_delete'),

    path('table/', views.table_view, name='table'),
    path('stats/', views.stats_view, name='stats'),
    path('reports/', views.reports_view, name='reports'),

]
