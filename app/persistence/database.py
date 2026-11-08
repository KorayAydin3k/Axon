from os import environ

from sqlalchemy import create_engine

engine = create_engine(environ["DATABASE_URL"], pool_pre_ping=True)
