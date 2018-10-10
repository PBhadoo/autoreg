#!/usr/local/bin/python3.6

import io as cStringIO
import re
import os
import sys

import psycopg2

import autoreg.dns.db

import unittest

class TestDnsDb(unittest.TestCase):
  re_do = re.compile('^; domain (\S+)$')
  re_zo = re.compile('^; zone (\S+)$')
  re_cr = re.compile('^; created: by (\S+), (\S+ \S+)$')
  re_up = re.compile('^; updated: by (\S+), (\S+ \S+)$')
  ns12 = """				NS	NS1.EU.ORG.
				NS	NS2.EU.ORG.
"""
  null = ""
  ns345 = """				NS	NS3.EU.ORG.
				NS	NS4.EU.ORG.
				NS	NS5.EU.ORG.
"""
  glue12 = """				NS	NS1.TESTGL.EU.ORG.
				NS	NS2.TESTGL.EU.ORG.
NS1.TESTGL			A	1.2.3.4
NS2.TESTGL			AAAA	::ffff:10.1.2.3
"""
  def _parseout(self, d, z):
    fout = cStringIO.StringIO()
    self.dd.show(d, z, outfile=fout)
    flags = []
    dom, zone, crby, upby = None, None, None, None
    rest = ''
    for l in fout.getvalue().split('\n'):
      if l == '; registry_hold' or l == '; registry_lock' or l == '; internal':
        flags.append(l[2:])
        continue
      if l == '; (NO RECORD)':
        continue
      m = self.re_do.match(l)
      if m:
        dom = m.groups()[0]
        continue
      m = self.re_zo.match(l)
      if m:
        zone = m.groups()[0]
        continue
      m = self.re_cr.match(l)
      if m:
        crby = m.groups()[0]
        continue
      m = self.re_up.match(l)
      if m:
        upby = m.groups()[0]
        continue
      rest += l + '\n'
    return dom, zone, crby, upby, flags, rest
  def _dropdb(self):
    self.dbc.execute("ABORT")
  def setUp(self):
    self.dbh = psycopg2.connect('dbname=test_autoreg_dev user=autoreg host=192.168.0.4 password=')
    dbc = self.dbh.cursor()
    self.dbh.set_isolation_level(0)
    dbc.execute("BEGIN")
    self.dd = autoreg.dns.db.db(dbc=dbc)
    self.dd.login('DNSADMIN')
    self.dbc = dbc

  def tearDown(self):
    del self.dd
    del self.dbh
    self._dropdb()
    del self.dbc
  def _test_base(self, dom, zone, internal, val1, val2):
    zone = 'EU.ORG'
    fqdn = dom + '.' + zone
    expect_flags = []
    if internal:
      expect_flags.append('internal')
    f1 = val1
    f2 = val2
    of1 = val1 + '\n'
    if val1 != '':
      of1 = dom + of1
    of2 = val2 + '\n'
    if val2 != '':
      of2 = dom + of2
    self.dd.new(fqdn, zone, 'NS', file=cStringIO.StringIO(f1),
                internal=internal)
    self.assertRaises(autoreg.dns.db.DomainError,
                      self.dd.new, fqdn, zone, 'NS',
                      file=cStringIO.StringIO(f1))
    self.assertEqual(self._parseout(fqdn, zone),
                     (fqdn, zone,
                      '*unknown*', '*unknown*', expect_flags, of1))
    self.dd.set_registry_lock(fqdn, zone, True)
    self.assertEqual(self._parseout(fqdn, zone),
                     (fqdn, zone,
                      '*unknown*', '*unknown*',
                      ['registry_lock'] + expect_flags,
                      of1))
    self.dd.set_registry_hold(fqdn, zone, True)
    self.assertEqual(self._parseout(fqdn, zone),
                     (fqdn, zone,
                      '*unknown*', '*unknown*',
                      ['registry_lock', 'registry_hold'] + expect_flags,
                      of1))
    self.assertRaises(autoreg.dns.db.AccessError,
                      self.dd.delete, fqdn, zone)
    self.assertRaises(autoreg.dns.db.AccessError,
                      self.dd.modify, fqdn, zone, 'NS',
                      file=cStringIO.StringIO(f2))
    self.dd.set_registry_lock(fqdn, zone, False)
    self.assertEqual(self._parseout(fqdn, zone),
                     (fqdn, zone,
                      '*unknown*', '*unknown*',
                      ['registry_hold'] + expect_flags, of1))
    if internal:
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.modify, fqdn, zone, 'NS',
                        file=cStringIO.StringIO(f2))
      self.assertEqual(self._parseout(fqdn, zone),
                       (fqdn, zone,
                        '*unknown*', '*unknown*',
                        ['registry_hold'] + expect_flags, of1))
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.delete, fqdn, zone)
      self.assertEqual(self._parseout(fqdn, zone),
                       (fqdn, zone,
                        '*unknown*', '*unknown*',
                        ['registry_hold'] + expect_flags, of1))
      self.dd.modify(fqdn, zone, 'NS',
                     file=cStringIO.StringIO(f2),
                     override_internal=True)
      self.assertEqual(self._parseout(fqdn, zone),
                       (fqdn, zone,
                        '*unknown*', '*unknown*',
                        ['registry_hold'] + expect_flags, of2))
      # "Domain is held" exceptions
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.delete, fqdn, zone, override_internal=True)
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.delete, fqdn, zone, override_internal=True,
                        grace_days=0)
      self.dd.set_registry_hold(fqdn, zone, False)
      self.dd.delete(fqdn, zone, override_internal=True, grace_days=0)
    else:
      self.dd.modify(fqdn, zone, 'NS', file=cStringIO.StringIO(f2))
      self.assertEqual(self._parseout(fqdn, zone),
                       (fqdn, zone,
                        '*unknown*', '*unknown*',
                        ['registry_hold'] + expect_flags, of2))
      # "Domain is held" exceptions
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.delete, fqdn, zone)
      self.assertRaises(autoreg.dns.db.AccessError,
                        self.dd.delete, fqdn, zone,
                        grace_days=0)
      self.dd.set_registry_hold(fqdn, zone, False)
      self.dd.delete(fqdn, zone, grace_days=0)
    self.assertRaises(autoreg.dns.db.DomainError,
                      self.dd.delete, fqdn, zone)
  def test1(self):
    self._test_base('TEST1', 'EU.ORG', False, self.ns12, self.ns345)
  def test1i(self):
    self._test_base('TEST1I', 'EU.ORG', True, self.ns12, self.ns345)
  def test2(self):
    self._test_base('TESTGL', 'EU.ORG', False, self.glue12, self.ns345)
  def test2c(self):
    self._test_base('TESTGL', 'EU.ORG', False, self.ns345, self.glue12)
  def test2u(self):
    self._test_base('TESTGL', 'EU.ORG', False, self.glue12, self.glue12)
  def test3a(self):
    self._test_base('TESTNL', 'EU.ORG', False, self.null, self.ns12)
  def test3b(self):
    self._test_base('TESTNL', 'EU.ORG', False, self.ns12, self.null)
            
if __name__ == '__main__':
  unittest.main()

