from os import environ, getenv

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

database_url = make_url(environ["DATABASE_URL"])
if database_host := getenv("AXON_DATABASE_HOST"):
    database_url = database_url.set(host=database_host)

engine = create_engine(database_url, pool_pre_ping=True)
