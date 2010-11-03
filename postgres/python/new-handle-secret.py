#!/usr/local/bin/python
#
# Generate a new secret seed for obfuscated email contact addresses.
# Cleanup expired secrets.
#
# To be run from time to time.
#

import psycopg2

import autoreg.conf

dbh = psycopg2.connect(autoreg.conf.dbstring)
dbc = dbh.cursor()
# use default expiration date for this table.
dbc.execute("INSERT INTO handle_secrets VALUES (NOW())")
assert dbc.rowcount == 1
dbc.execute("DELETE FROM handle_secrets WHERE expires < NOW()")
dbh.commit()
