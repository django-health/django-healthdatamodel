from django.db import models


class DataSource(models.TextChoices):
    APPLE_HEALTH = "apple_health", "Apple Health"
    FITBIT = "fitbit", "Fitbit"
    GARMIN = "garmin", "Garmin"
    GOOGLE_HEALTH = "google_health", "Google Health"
    HEALTH_CONNECT = "health_connect", "Health Connect"
    OURA = "oura", "Oura"
    STRAVA = "strava", "Strava"
    WHOOP = "whoop", "WHOOP"


class DeviceBrand(models.TextChoices):
    APPLE = "apple", "Apple"
    SAMSUNG = "samsung", "Samsung"
    FITBIT = "fitbit", "Fitbit"
    GARMIN = "garmin", "Garmin"
    OURA = "oura", "Oura"
    WHOOP = "whoop", "WHOOP"
    DATAJET = "datajet", "DataJet"  # this is for testing


class ConnectionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"
