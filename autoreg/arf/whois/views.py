# $Id$

import crypt
import datetime
import io
import random
import re
import socket
import time

import psycopg2

from autoreg.whois.db import HANDLESUFFIX, \
  suffixstrip,suffixadd,Domain,check_handle_domain_auth,handle_domains_dnssec, \
  countries_get, country_from_name, \
  admin_login
from autoreg.arf.arf.settings import URIBASE, URLBASE
from autoreg.arf.util import render_to_mail
from autoreg.common import domain_delete
from autoreg.conf import FROMADDR
import autoreg.dns.db

import django.contrib.auth
from django.core.exceptions import SuspiciousOperation, PermissionDenied
from django.core.urlresolvers import reverse
from django.http import HttpResponse, \
  HttpResponseRedirect, HttpResponseForbidden
from django.shortcuts import render_to_response
from django import forms
from django.forms.widgets import PasswordInput
from django.views.decorators.cache import cache_control
from django.db import connection
from django.template import RequestContext

from models import Whoisdomains,Contacts,Tokens,DomainContact

URILOGIN = URIBASE + 'login/'
RESET_TOKEN_HOURS_TTL = 24
EMAIL_TOKEN_HOURS_TTL = 72
VAL_TOKEN_HOURS_TTL = 72
RESET_TOKEN_TTL = RESET_TOKEN_HOURS_TTL*3600
EMAIL_TOKEN_TTL = EMAIL_TOKEN_HOURS_TTL*3600
VAL_TOKEN_TTL = VAL_TOKEN_HOURS_TTL*3600

domcontact_choices = [('technical', 'technical'),
                      ('administrative', 'administrative'),
                      ('zone', 'zone')]

# chars allowed in passwords or reset/validation tokens
allowed_chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'

#
# Helper functions
#

# parameters for SHA512 hashed passwords
CRYPT_SALT_LEN=16
CRYPT_ALGO='$6$'

def _pwcrypt(passwd):
  """Compute a crypt(3) hash suitable for user authentication"""
  # Make a salt
  salt_chars = '0123456789abcdefghijklmnopqstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ/.'
  t = ''.join(random.SystemRandom().choice(salt_chars) \
              for i in range(CRYPT_SALT_LEN))
  return crypt.crypt(passwd, CRYPT_ALGO + t + '$')

def _token_find(contact_id, action):
  """Find existing token(s)"""
  # Expire old tokens beforehand
  dbc = connection.cursor()
  dbc.execute('DELETE FROM arf_tokens WHERE expires < NOW()')
  return Tokens.objects.filter(contact_id=contact_id, action=action)

def _token_clear(contact_id, action):
  """Cleanup pre-existing token(s)"""
  _token_find(contact_id, action).delete()

def _token_set(contact_id, action, args=None, ttl=3600):
  """Create a token for the indicated action on the indicated contact"""
  sr = random.SystemRandom()
  token = ''.join(sr.choice(allowed_chars) for i in range(16))
  t = time.time()
  now = datetime.datetime.fromtimestamp(t)
  expires = datetime.datetime.fromtimestamp(t + ttl)
  tk = Tokens(contact_id=contact_id, date=now, expires=expires,
              token=token, action=action, args=args)
  tk.save()
  return token

#
# Forms
#

class contactbyemail_form(forms.Form):
  email = forms.EmailField(max_length=100)

class contactbyhandle_form(forms.Form):
  handle = forms.CharField(max_length=15, initial=HANDLESUFFIX, help_text='Your handle')

class contactbydomain_form(forms.Form):
  domain = forms.CharField(max_length=80, initial='.eu.org', help_text='Domain')

class contactchange_form(forms.Form):
  pn1 = forms.RegexField(max_length=60, label="Name", regex='^[a-zA-Z \.-]+\s+[a-zA-Z \.-]')
  em1 = forms.EmailField(max_length=64, label="E-mail")
  ad1 = forms.CharField(max_length=80, label="Organization")
  ad2 = forms.CharField(max_length=80, label="Address (line 1)")
  ad3 = forms.CharField(max_length=80, label="Address (line 2)", required=False)
  ad4 = forms.CharField(max_length=80, label="Address (line 3)", required=False)
  ad5 = forms.CharField(max_length=80, label="Address (line 4)", required=False)
  ad6 = forms.ChoiceField(initial='', label="Country (required)",
                          choices=countries_get(connection.cursor()))
  ph1 = forms.RegexField(max_length=30, label="Phone Number", regex='^\+?[\d\s#\-\(\)\[\]\.]+$', required=False)
  fx1 = forms.RegexField(max_length=30, label="Fax Number", regex='^\+?[\d\s#\-\(\)\[\]\.]+$', required=False)
  private = forms.BooleanField(label="Hide address/phone/fax in public whois", required=False)

class contact_form(contactchange_form):
  p1 = forms.CharField(max_length=20, label='Password', required=False, widget=PasswordInput)
  p2 = forms.CharField(max_length=20, label='Confirm Password', required=False, widget=PasswordInput)
  policy = forms.BooleanField(label="I accept the Policy", required=True)

class registrant_form(forms.Form):
  # same as contactchange_form minus the email field
  pn1 = forms.RegexField(max_length=60, label="Name", regex='^[a-zA-Z \.-]+\s+[a-zA-Z \.-]')
  # disabled until we get rid of the RIPE model (unshared registrant records)
  #em1 = forms.EmailField(max_length=64, label="E-mail", required=False)
  ad1 = forms.CharField(max_length=80, label="Organization")
  ad2 = forms.CharField(max_length=80, label="Address (line 1)")
  ad3 = forms.CharField(max_length=80, label="Address (line 2)", required=False)
  ad4 = forms.CharField(max_length=80, label="Address (line 3)", required=False)
  ad5 = forms.CharField(max_length=80, label="Address (line 4)", required=False)
  ad6 = forms.ChoiceField(initial='', label="Country (required)",
                          choices=countries_get(connection.cursor()))
  ph1 = forms.RegexField(max_length=30, label="Phone Number", regex='^\+?[\d\s#\-\(\)\[\]\.]+$', required=False)
  fx1 = forms.RegexField(max_length=30, label="Fax Number", regex='^\+?[\d\s#\-\(\)\[\]\.]+$', required=False)
  private = forms.BooleanField(label="Hide address/phone/fax in public whois", required=False)

class domcontact_form(forms.Form):
  contact_type = forms.ChoiceField(choices=domcontact_choices, label="type")
  handle = forms.CharField(max_length=10, initial=HANDLESUFFIX)

class contactlogin_form(forms.Form):
  handle = forms.CharField(max_length=15, initial=HANDLESUFFIX, help_text='Your handle')
  password = forms.CharField(max_length=30, help_text='Your password', widget=PasswordInput)

class resetpass_form(forms.Form):
  resettoken = forms.CharField(max_length=30, label='Reset Token')
  pass1 = forms.CharField(max_length=20, label='New Password', widget=PasswordInput)
  pass2 = forms.CharField(max_length=20, label='Confirm Password', widget=PasswordInput)

class changemail_form(forms.Form):
  token = forms.CharField(max_length=30)

class chpass_form(forms.Form):
  pass0 = forms.CharField(max_length=30, label='Current Password', widget=PasswordInput)
  pass1 = forms.CharField(max_length=30, label='New Password', widget=PasswordInput)
  pass2 = forms.CharField(max_length=30, label='Confirm Password', widget=PasswordInput)

#
# 'view' functions called from urls.py and friends
#

#
# public pages
#

def login(request):
  """Login page"""
  if request.method == "GET":
    next = request.GET.get('next', URIBASE)
    if request.user.is_authenticated():
      return HttpResponseRedirect(next)
    f = contactlogin_form()
    form = f.as_table()
    request.session.set_test_cookie()
    if next == URIBASE:
      next = None
    vars = RequestContext(request,
                          {'form': form, 'posturi': request.path, 'next': next})
    return render_to_response('whois/login.html', vars)
  elif request.method == "POST":
    next = request.POST.get('next', URIBASE)
    vars = {'next': next}
    if request.user.is_authenticated():
      #django.contrib.auth.logout(request)
      return HttpResponseRedirect(next)
      #return HttpResponse('OK')
    if not request.session.test_cookie_worked():
      vars['msg'] = "Please enable cookies"
      vars['form'] = contactlogin_form().as_table()
      vars = RequestContext(request, vars)
      return render_to_response('whois/login.html', vars)
    else:
      #request.session.delete_test_cookie()
      pass
    handle = request.POST.get('handle', '').upper()
    password = request.POST.get('password', '')
    handle = suffixstrip(handle)

    vars = {'posturi': request.path, 'next': request.path,
            'form': contactlogin_form().as_table()}
    user = django.contrib.auth.authenticate(username=handle, password=password)
    if user is not None:
      c = Contacts.objects.filter(handle=handle)
      if c.count() != 1:
        raise SuspiciousOperation
      v = c[0].validated_on
      if not v:
        vars['msg'] = "You need to validate your account. " \
                      "Please check your e-mail for the validation link."
      elif user.is_active:
        django.contrib.auth.login(request, user)
        return HttpResponseRedirect(next)
      else:
        vars['msg'] = "Sorry, your account has been disabled"
    else:
      vars['msg'] = "Your username and/or password is incorrect"
    vars = RequestContext(request, vars)
    return render_to_response('whois/login.html', vars)
  else:
    raise SuspiciousOperation

def contactbydomain(request):
  if request.method == "GET":
    f = contactbydomain_form()
    form = f.as_table()
    vars = RequestContext(request, {'form': form})
    return render_to_response('whois/contactdomainform.html', vars)
  elif request.method == "POST":
    fqdn = request.POST.get('domain', '')
    handles = DomainContact.objects \
               .filter(whoisdomain_id__fqdn=fqdn.upper(),
                       contact_id__email__isnull=False) \
               .distinct().values_list('contact_id__handle', flat=True)
    vars = RequestContext(request, {'handles': handles,
                                    'suffix': HANDLESUFFIX })
    return render_to_response('whois/contactdomain.html', vars)
  else:
    raise SuspiciousOperation

def makeresettoken(request, handle=None):
  """Password reset step 1: send a reset token to the contact email address"""
  if request.method == "GET":
    if handle:
      f = contactbyhandle_form(initial={ 'handle': suffixadd(handle) })
    else:
      f = contactbyhandle_form()
    form = f.as_table()
    vars = RequestContext(request, {'form': form, 'posturi': request.path})
    return render_to_response('whois/resetpass.html', vars)
  elif request.method == "POST":
    handle = request.POST.get('handle', '').upper()
    fullhandle = handle
    handle = suffixstrip(handle)
    ctl = Contacts.objects.filter(handle=handle)
    if len(ctl) == 0:
      vars = RequestContext(request,
                            {'posturi': request.path,
                             'ehandle': suffixadd(handle),
                             'next': request.path})
      return render_to_response('whois/contactnotfound.html', vars)
    if len(ctl) != 1:
      raise SuspiciousOperation
    ct = ctl[0]

    # create new token
    _token_clear(ct.id, action="pwreset")
    token = _token_set(ct.id, action="pwreset", ttl=RESET_TOKEN_TTL)

    if not render_to_mail('whois/resetpass.mail',
                           { 'from': FROMADDR, 'to': ct.email,
                             'urlbase': URLBASE,
                             'handle': fullhandle,
                             'handleshort': handle,
                             'token': token }, FROMADDR, [ ct.email ]):
       vars = RequestContext(request,
         {'msg': "Sorry, error while sending mail. Please try again later."})
       return render_to_response('whois/msgnext.html', vars)
    vars = RequestContext(request, {'ehandle': suffixadd(handle)})
    return render_to_response('whois/tokensent.html', vars)

def resetpass2(request, handle):
  """Password reset step 2:
     check provided reset token and force indicated password
     on the designated contact."""
  f = resetpass_form()
  form = f.as_table()
  vars = {'form': form, 'posturi': request.path}
  if request.method == "GET":
    vars = RequestContext(request, vars)
    return render_to_response('whois/resetpass2.html', vars)
  elif request.method == "POST":
    ctl = Contacts.objects.filter(handle=handle)
    if len(ctl) < 1:
      vars = RequestContext(request, vars)
      return render_to_response('whois/resetpass2.html', vars)
    ct = ctl[0]
    pass1 = request.POST.get('pass1', 'A')
    pass2 = request.POST.get('pass2', 'B')
    if pass1 != pass2:
      vars['msg'] = "They don't match, try again"
      vars = RequestContext(request, vars)
      return render_to_response('whois/resetpass2.html', vars)
    if len(pass1) < 8:
      vars['msg'] = "Password should be at least 8 chars"
      vars = RequestContext(request, vars)
      return render_to_response('whois/resetpass2.html', vars)
    token = request.POST.get('resettoken', 'C')
    tkl = _token_find(ct.id, "pwreset")
    if len(tkl) > 1:
      raise SuspiciousOperation
    if len(tkl) == 0 or token != tkl[0].token:
      vars['msg'] = "Invalid reset token"
      vars = RequestContext(request, vars)
      return render_to_response('whois/resetpass2.html', vars)
    tk = tkl[0]
    ct.passwd = _pwcrypt(pass1)
    ct.save()
    tk.delete()
    vars = RequestContext(request, {'ehandle': suffixadd(handle)})
    return render_to_response('whois/passchanged.html', vars)

def contactcreate(request):
  """Contact creation page"""
  if request.user.is_authenticated():
    handle = request.user.username
  else:
    handle = None
  p_errors = []
  if request.method == "GET":
    form = contact_form()
  elif request.method == "POST":
    form = contact_form(request.POST)

    # validate password field by hand
    p1 = request.POST.get('p1', '')
    p2 = request.POST.get('p2', '')
    if p1 != p2:
      p_errors = ["Passwords don't match"]
    elif len(p1) < 8:
      p_errors = ["Password too short"]

    if form.is_valid() and not p_errors:
      #
      # Process contact creation
      #
      d = {}
      for i in ['pn', 'em', 'ph', 'fx']:
        v = form.cleaned_data.get(i + '1', None)
        if v != '':
          d[i] = [v]
      ad = []
      for i in ['ad1', 'ad2', 'ad3', 'ad4', 'ad5']:
        a = form.cleaned_data.get(i, None)
        if a is not None and a != '':
          ad.append(a)
      co = form.cleaned_data.get('ad6', None)
      if co is not None and co != '':
        d['co'] = [ co ]
      d['ad'] = ad
      d['ch'] = [(request.META.get('REMOTE_ADDR', 'REMOTE_ADDR_NOT_SET'), None)]
      private = form.cleaned_data['private']

      from autoreg.whois.db import Person

      p = Person(connection.cursor(), passwd=_pwcrypt(p1), private=private,
                 validate=False)
      if p.from_ripe(d):
        p.insert()
        valtoken = _token_set(p.cid, "contactval", ttl=VAL_TOKEN_TTL)
        ehandle = suffixstrip(p.gethandle())
        if not render_to_mail('whois/contactcreate.mail',
                               {'urlbase': URLBASE,
                                'handleshort': ehandle.upper(),
                                'valtoken': valtoken,
                                'whoisdata': p.__str__(),
                                'from': FROMADDR, 'to': d['em'][0],
                                'handle': suffixadd(ehandle)},
                               FROMADDR, [d['em'][0]]):
          vars = RequestContext(request,
           {'msg': "Sorry, error while sending mail. Please try again later."})
          return render_to_response('whois/msgnext.html', vars)
        vars = RequestContext(request,
               {'msg': "Contact successfully created as %s. Please check instructions sent to %s to validate it." % (suffixadd(ehandle), d['em'][0])})
        return render_to_response('whois/msgnext.html', vars)
      # else: fall through
  vars = RequestContext(request,
                        {'form': form, 'posturi': request.path,
                         'handle': suffixadd(handle) if handle else None,
                         'p_errors': p_errors})
  return render_to_response('whois/contactcreate.html', vars)

def contactvalidate(request, handle, valtoken):
  """Contact validation page"""
  if request.user.is_authenticated():
    django.contrib.auth.logout(request)

  # XXX: strange bug causes a SIGSEGV if we use valtoken from URL parsing
  # after a POST; instead we pass it as an hidden FORM variable,
  # hence the following two lines.
  if request.method == "POST":
    valtoken = request.POST.get('valtoken')

  msg = None
  ctl = Contacts.objects.filter(handle=handle)
  if len(ctl) != 1:
    msg = "Sorry, contact handle or validation token is not valid."
  else:
    tkl = _token_find(ctl[0].id, "contactval")
    if len(tkl) != 1 or tkl[0].token != valtoken:
      msg = "Sorry, contact handle or validation token is not valid."
  if msg:
    vars = RequestContext(request, {'msg': msg})
    return render_to_response('whois/msgnext.html', vars)
  ct = ctl[0]
  if request.method == "GET":
    vars = RequestContext(request,
            {'handle': suffixadd(handle), 'email': ct.email,
            'valtoken': valtoken, 'posturi': request.path})
    return render_to_response('whois/contactvalidate.html', vars)
  elif request.method == "POST":
    ct.validated_on = datetime.datetime.today()
    ct.save()
    tkl[0].delete()
    vars = RequestContext(request,
                          {'msg': "Your contact handle is now valid."})
    return render_to_response('whois/msgnext.html', vars)
  raise SuspiciousOperation

def domain(request, fqdn):
  """Whois from domain FQDN"""
  f = fqdn.upper()
  try:
    dom = Whoisdomains.objects.get(fqdn=f)
  except Whoisdomains.DoesNotExist:
    dom = None
  if dom is None:
    vars = RequestContext(request, {'fqdn': fqdn})
    return render_to_response('whois/domainnotfound.html', vars)
  cl = dom.domaincontact_set.all()
  vars = RequestContext(request, {'whoisdomain': dom, 'domaincontact_list': cl})
  return render_to_response('whois/fqdn.html', vars)

# private pages

@cache_control(private=True)
def chpass(request):
  """Contact password change"""
  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  handle = request.user.username
  f = chpass_form()
  form = f.as_table()
  vars = {'form': form, 'posturi': request.path, 'handle': suffixadd(handle)}
  if request.method == "GET":
    vars = RequestContext(request, vars)
    return render_to_response('whois/chpass.html', vars)
  elif request.method == "POST":
    pass0 = request.POST.get('pass0', '')
    pass1 = request.POST.get('pass1', '')
    pass2 = request.POST.get('pass2', '')
    if pass1 != pass2:
      vars['msg'] = "They don't match, try again"
      vars = RequestContext(request, vars)
      return render_to_response('whois/chpass.html', vars)
    if len(pass1) < 8:
      vars['msg'] = "Password should be at least 8 chars"
      vars = RequestContext(request, vars)
      return render_to_response('whois/chpass.html', vars)

    ctlist = Contacts.objects.filter(handle=handle)
    if len(ctlist) != 1:
      raise SuspiciousOperation

    ct = ctlist[0]
    if ct.passwd != crypt.crypt(pass0, ct.passwd):
      vars['msg'] = "Current password is not correct"
      vars = RequestContext(request, vars)
      return render_to_response('whois/chpass.html', vars)
    ct.passwd = _pwcrypt(pass1)
    ct.save()
    del vars['form']
    vars['ehandle'] = vars['handle']
    vars = RequestContext(request, vars)
    return render_to_response('whois/passchanged.html', vars)

@cache_control(private=True)
def domainlist(request):
  """Display domain list for current contact"""
  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  handle = request.user.username

  domds = handle_domains_dnssec(connection.cursor(), handle)

  vars = RequestContext(request,
           {'posturi': request.path, 'handle': suffixadd(handle),
            'domds': domds})
  return render_to_response('whois/domainlist.html', vars)

@cache_control(private=True)
def contactchange(request, registrantdomain=None):
  """Contact or registrant modification page.
     If registrant, registrantdomain contains the associated domain FQDN.
  """
  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  if registrantdomain and registrantdomain != registrantdomain.lower():
    return HttpResponseRedirect(reverse(contactchange,
                                        args=[registrantdomain.lower()]))
  handle = request.user.username
  if registrantdomain:
    # check handle is authorized on domain
    if not check_handle_domain_auth(connection.cursor(),
                                    handle + HANDLESUFFIX, registrantdomain) \
     and not admin_login(connection.cursor(), request.user.username):
      return HttpResponseForbidden("Unauthorized")
    dom = Whoisdomains.objects.get(fqdn=registrantdomain.upper())
    cl = dom.domaincontact_set.filter(contact_type__name='registrant')
    if len(cl) != 1:
      raise SuspiciousOperation
    ehandle = cl[0].contact.handle
  else:
    ehandle = handle

  vars = {'posturi': request.path, 'handle': suffixadd(handle)}
  if request.method == "GET":
    c = Contacts.objects.get(handle=ehandle)
    adlist = c.addr.rstrip().split('\n')
    initial = { 'pn1': c.name,
                'em1': c.email,
                'ph1': c.phone,
                'fx1': c.fax,
                'private': c.private }
    n = 1
    lastk = None
    for i in adlist:
      lastk = 'ad%d' % n
      initial[lastk] = i
      n += 1
    if c.country is not None:
      initial['ad6'] = c.country
    elif lastk and lastk != 'ad6':
      co = country_from_name(initial[lastk])
      if co:
        # For "legacy" contact records, if the last address line
        # looks like a country, convert it to an ISO country code
        # and move it to the 'ad6' field in the form.
        initial['ad6'] = co
        del initial[lastk]
    if registrantdomain:
      vars['domain'] = registrantdomain.upper()
      vars['form'] = registrant_form(initial=initial)
    else:
      vars['ehandle'] = suffixadd(ehandle)
      vars['form'] = contactchange_form(initial=initial)
    vars = RequestContext(request, vars)
    return render_to_response('whois/contactchange.html', vars)
  elif request.method == "POST":
    if registrantdomain:
      form = registrant_form(request.POST)
    else:
      form = contactchange_form(request.POST)
    if form.is_valid():
      c = Contacts.objects.get(handle=ehandle)
      ad = []
      for i in '12345':
        k = 'ad%c' % i
        if form.cleaned_data[k] != '':
          ad.append(form.cleaned_data[k]) 
      changed = False
      emailchanged = False
      if c.name != form.cleaned_data['pn1']:
        c.name = form.cleaned_data['pn1']
        changed = True
      if ('em1' in form.cleaned_data
          and form.cleaned_data['em1'] != ''
          and c.email != form.cleaned_data['em1']):
        newemail = form.cleaned_data['em1']
        emailchanged = True
      for i in ['fx1', 'ph1']:
        if form.cleaned_data[i] == '':
          form.cleaned_data[i] = None
      if c.phone != form.cleaned_data['ph1']:
        c.phone = form.cleaned_data['ph1']
        changed = True
      if c.fax != form.cleaned_data['fx1']:
        c.fax = form.cleaned_data['fx1']
        changed = True
      if c.country != form.cleaned_data['ad6']:
        c.country = form.cleaned_data['ad6']
        changed = True
      if c.addr != '\n'.join(ad):
        c.addr = '\n'.join(ad)
        changed = True
      if c.private != form.cleaned_data['private']:
        c.private = form.cleaned_data['private']
        changed = True
      if changed:
        c.updated_on = None	# set to NOW() by the database
        c.updated_by = suffixadd(request.user.username)
        c.save()
      if emailchanged:
        _token_clear(c.id, "changemail")
        token = _token_set(c.id, "changemail", newemail, EMAIL_TOKEN_TTL)
        if not render_to_mail('whois/changemail.mail',
                               {'from': FROMADDR, 'to': newemail,
                                'urlbase': URLBASE,
                                'handle': suffixadd(ehandle),
                                'newemail': newemail,
                                'token': token }, FROMADDR, [ newemail ]):
          vars = RequestContext(request,
            {'msg': "Sorry, error while sending mail. Please try again later."})
          return render_to_response('whois/msgnext.html', vars)
        return HttpResponseRedirect(reverse(changemail))
      if registrantdomain:
        return HttpResponseRedirect(reverse(domainedit,
                                            args=[registrantdomain]))
      else:
        vars['msg'] = "Contact information changed successfully"
        vars = RequestContext(request, vars)
        return render_to_response('whois/msgnext.html', vars)
    else:
      vars['form'] = form
      vars = RequestContext(request, vars)
      return render_to_response('whois/contactchange.html', vars)

@cache_control(private=True)
def changemail(request):
  """Email change step 2:
     check provided change email token and force indicated email
     on the designated contact."""
  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  handle = request.user.username
  f = changemail_form()
  form = f.as_table()
  vars = {'form': form, 'posturi': request.path, 'handle': suffixadd(handle)}

  ctl = Contacts.objects.filter(handle=handle)
  if len(ctl) != 1:
    raise SuspiciousOperation
  ct = ctl[0]
  tkl = _token_find(ct.id, "changemail")
  if len(tkl) > 1:
    raise SuspiciousOperation
  if len(tkl) == 0:
      vars['msg'] = "Sorry, didn't find any waiting email address change."
      vars = RequestContext(request, vars)
      return render_to_response('whois/changemail.html', vars)
  tk = tkl[0]

  vars['newemail'] = tk.args

  if request.method == "GET":
    vars = RequestContext(request, vars)
    return render_to_response('whois/changemail.html', vars)
  elif request.method == "POST":
    token = request.POST.get('token', 'C')
    if token != tk.token:
      vars['msg'] = "Invalid token"
      vars = RequestContext(request, vars)
      return render_to_response('whois/changemail.html', vars)
    newemail = tk.args
    ct.email = newemail
    ct.save()
    tk.delete()
    vars = RequestContext(request, vars)
    return render_to_response('whois/emailchanged.html', vars)

@cache_control(private=True)
def domaineditconfirm(request, fqdn):
  """Request confirmation for self-deletion of a contact"""
  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  if fqdn != fqdn.lower():
    return HttpResponseRedirect(reverse(domaineditconfirm, args=[fqdn.lower()]))
  nexturi = reverse(domainedit, args=[fqdn])
  vars = {'fqdn': fqdn.upper(), 'handle': suffixadd(request.user.username),
          'posturi': nexturi}
  contact_type = request.POST.get('contact_type', None)
  handle = request.POST.get('handle', None)
  if request.method == "POST" and contact_type and handle:
    vars.update({'contact_type': contact_type, 'handle': handle})
    vars = RequestContext(request, vars)
    return render_to_response('whois/domaineditconfirm.html', vars)
  else:
    return HttpResponseRedirect(nexturi)

@cache_control(private=True)
def domainedit(request, fqdn):
  """Edit domain contacts"""
  # list of shown and editable contact types
  typelist = ["administrative", "technical", "zone"]

  if not request.user.is_authenticated():
    return HttpResponseRedirect((URILOGIN + '?next=%s') % request.path)
  handle = request.user.username

  if fqdn != fqdn.lower():
    return HttpResponseRedirect(reverse(domainedit, args=[fqdn.lower()]))

  f = fqdn.upper()
  try:
    dom = Whoisdomains.objects.get(fqdn=f)
  except Whoisdomains.DoesNotExist:
    dom = None
  if dom is None:
    vars = RequestContext(request, {'fqdn': fqdn})
    return render_to_response('whois/domainnotfound.html', vars)

  domds = handle_domains_dnssec(connection.cursor(), handle, fqdn)
  if len(domds) != 1:
    raise SuspiciousOperation
  has_ns, has_ds, can_ds = domds[0][1], domds[0][7], domds[0][2]
  registry_hold, end_grace_period = domds[0][5], domds[0][6]

  # check handle is authorized on domain
  if not check_handle_domain_auth(connection.cursor(), handle + HANDLESUFFIX, f) \
     and not admin_login(connection.cursor(), request.user.username):
    return HttpResponseForbidden("Unauthorized")

  dbdom = Domain(connection.cursor(), did=dom.id)
  dbdom.fetch()

  msg = None

  if request.method == "POST":
    if 'submit' in request.POST \
        or 'submitd' in request.POST \
        or 'submita' in request.POST:
      contact_type = request.POST['contact_type']
      chandle = suffixstrip(request.POST['handle'])
      ctl = Contacts.objects.filter(handle=chandle)
      if len(ctl) == 0:
        msg = "Contact %s not found" % suffixadd(chandle)
      elif len(ctl) != 1:
        raise SuspiciousOperation
      else:
        cid = ctl[0].id
        if contact_type[0] not in 'atz':
          raise SuspiciousOperation
        code = contact_type[0] + 'c'
        if 'submit' in request.POST \
           and (request.POST['submit'] == 'Delete' \
            or request.POST['submit'] == 'Confirm Delete') \
           or 'submitd' in request.POST:
          if cid in dbdom.d[code]:
            numcontacts = 0
            for i in 'atz':
              numcontacts += len(dbdom.d[i+'c'])
            if numcontacts == 1:
              # Refuse deletion of the last contact
              msg = "Sorry, must leave at least one contact!"
            else:
              dbdom.d[code].remove(cid)
              dbdom.update()
          else:
            msg = "%s is not a contact" % suffixadd(chandle)
          # Fall through to updated form display
        elif 'submit' in request.POST and request.POST['submit'] == 'Add' \
            or 'submita' in request.POST:
          if cid not in dbdom.d[code]:
            dbdom.d[code].append(cid)
            dbdom.update()
          else:
            msg = "%s is already a %s contact" % (chandle, contact_type)
          # Fall through to updated form display
        elif 'submit' in request.POST and request.POST['submit'] == 'Cancel':
          # Fall through to updated form display
          pass
    else:
      raise SuspiciousOperation
  elif request.method != "GET":
    raise SuspiciousOperation

  # handle GET or end of POST

  # get contact list
  cl = dom.domaincontact_set.order_by('contact_type', 'contact__handle')
  formlist = []
  for c in cl:
    ct = c.contact_type.name
    if ct in typelist:
      cthandle = c.contact.handle
      if cthandle == handle:
        posturi = reverse(domaineditconfirm, args=[f.lower()])
      else:
        posturi = request.path
      formlist.append({'contact_type': ct,
                       'handle': suffixadd(cthandle),
                       'posturi': posturi })

  vars = {'whoisdomain': dom, 'domaincontact_list': cl,
          'msg': msg,
          'formlist': formlist,
          'handle': suffixadd(handle),
          'whoisdisplay': unicode(dbdom),
          'has_ns': has_ns, 'has_ds': has_ds, 'can_ds': can_ds,
          'registry_hold': registry_hold, 'end_grace_period': end_grace_period,
          'addform': {'posturi': request.path,
                      'domcontact_form': domcontact_form()}}
  vars = RequestContext(request, vars)
  return render_to_response('whois/domainedit.html', vars)

@cache_control(private=True)
def domaindelete(request, fqdn):
  if request.method != "POST":
    raise SuspiciousOperation
  if not request.user.is_authenticated():
    raise PermissionDenied
  if fqdn != fqdn.lower():
    return HttpResponseRedirect(reverse(domaindelete, args=[fqdn.lower()]))
  if not check_handle_domain_auth(connection.cursor(),
                                  request.user.username, fqdn):
    return HttpResponseForbidden("Unauthorized")

  dbh = psycopg2.connect(autoreg.conf.dbstring)
  dd = autoreg.dns.db.db(dbh)
  dd.login('autoreg')

  out = io.StringIO()

  err, ok = None, False
  try:
    ok = domain_delete(dd, fqdn, out, None)
  except autoreg.dns.db.AccessError as e:
    err = unicode(e)
  except autoreg.dns.db.DomainError as e:
    err = unicode(e)

  # release the write lock on the zone record.
  dbh.commit()

  if not ok or err:
    if err:
      msg = err;
    else:
      msg = 'Sorry, domain deletion failed, please try again later.'
    vars = RequestContext(request, {'msg': msg})
    return render_to_response('whois/msgnext.html', vars)

  return HttpResponseRedirect(reverse(domainedit, args=[fqdn.lower()]))

@cache_control(private=True)
def domainundelete(request, fqdn):
  if request.method != "POST":
    raise SuspiciousOperation
  if not request.user.is_authenticated():
    raise PermissionDenied
  if fqdn != fqdn.lower():
    raise PermissionDenied
  if not check_handle_domain_auth(connection.cursor(),
                                  request.user.username, fqdn):
    return HttpResponseForbidden("Unauthorized")

  dbh = psycopg2.connect(autoreg.conf.dbstring)
  dd = autoreg.dns.db.db(dbh)
  dd.login('autoreg')

  dd.undelete(fqdn, None)

  return HttpResponseRedirect(reverse(domainedit, args=[fqdn.upper()]))

def logout(request):
  """Logout page"""
  django.contrib.auth.logout(request)
  return HttpResponseRedirect(reverse(login))
