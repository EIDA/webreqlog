#*****************************************************************************
# webreqlog.py
#
# Small WSGI - WebServer for the display of
# Arclink Request Logs
#
#
# (c) 2010 Mathias Hoffmann, GFZ Potsdam
# 2020 ported to Seiscomp 4 and python3 by Stefan Heimers
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2, or (at your option) any later
# version. For more information, see http://www.gnu.org/
#
# wget http://www.highcharts.com/downloads/zips/Highcharts-1.2.5.zip
#
#*****************************************************************************

from io import StringIO
import os, sys, re, gc, socket
from datetime import datetime, timedelta
from wsgiref.simple_server import make_server
from seiscomp import logs
import seiscomp.core, seiscomp.client, seiscomp.system, seiscomp.datamodel, seiscomp.logging, seiscomp.io
import uuid
import smtplib
from email.mime.text import MIMEText

VERSION = "1.0.3 (2020.203)"
EMAIL_FROM =  socket.gethostname() + "." + os.getenv("LOGNAME").replace("LOGNAME=","") + "@sed.ethz.ch"
EMAIL_SMTP = "localhost"


#----------------------------------------------------------------------------------------------------
def byte2h(bytes, bytesOnly=None):
	if bytesOnly:
		return "%d Bytes" % bytes
	if bytes < 1.0E3:
		return "%d B" % (bytes/1024.0**0)
	elif bytes < 1.0E6:
		return "%.1f KiB" % (bytes/1024.0**1)
	elif bytes < 1.0E9:
		return "%.1f MiB" % (bytes/(1024.0**2))
	elif bytes < 1.0E12:
		return "%.1f GiB" % (bytes/(1024.0**3))
	elif bytes < 1.0E15:
		return "%.1f TiB" % (bytes/(1024.0**4))
	else:
		return "%d B" % bytes
#----------------------------------------------------------------------------------------------------

#----------------------------------------------------------------------------------------------------
def sec2h(s, secsOnly=None):
	if secsOnly:
		return "%ds" % s
	if s < 60:
		return "%ds" % s
	elif s < 3600:
		m = int(s/60)
		s = int(s-m*60)
		return "%dm %ds" % (m,s)
	elif s < 86400:
		h = int(s/3600)
		m = int((s-h*3600)/60)
		return "%dh %dm" % (h,m)
	elif s < 86400*365:
		d = int(s/86400)
		h = int((s-d*86400)/3600)
		return "%dd %dh" % (d,h)
	elif s >= 31536000:
		y = int(s/31536000)
		d = int((s-y*31536000)/86400)
		return "%dy %dd" % (y,d)
	else:
		return "%ds" % s
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
dateTimeRe = (
	"^(?P<datetime>\d{4}-\d{1,2}-\d{1,2}\s{1}\d{1,2}:\d{1,2}:\d{1,2})$",
	"^(?P<date>\d{4}-\d{1,2}-\d{1,2})$",
	"^(?P<time>\d{1,2}:\d{1,2}:\d{1,2})$"
	)
dateTimeSelector = re.compile("|".join(dateTimeRe))

def str2date(line):
	m = dateTimeSelector.search(line.replace("%20", " "))
	if m:
		d = m.groupdict()
		if d["datetime"]:
			return seiscomp.core.Time.FromString(d["datetime"], "%Y-%m-%d %H:%M:%S")
		if d["date"]:
			return seiscomp.core.Time.FromString(d["date"], "%Y-%m-%d")
		if d["time"]:
			now = datetime.now()
			t = d["time"].split(":") # FIXME
			return seiscomp.core.Time(now.year, now.month, now.day, int(t[0]), int(t[1]), int(t[2]))

	print ("str2date(%s): " % line)
	raise Exception
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
def date2str(date, mode='date time'):
	if mode == 'date time': return date.toString("%Y-%m-%d %H:%M:%S")
	if mode == 'date_time': return date.toString("%Y-%m-%d_%H:%M:%S")
	if mode == 'date': return date.toString("%Y-%m-%d")
	if mode == 'time': return date.toString("%H:%M:%S")
#----------------------------------------------------------------------------------------------------

#----------------------------------------------------------------------------------------------------
html_escape_table = {
	"&": "&amp;",
	'"': "&quot;",
	"'": "&apos;",
	">": "&gt;",
	"<": "&lt;",
	}

def html_escape(text):
	"""Produce a string with entities which can be safely included in a valid HTML document.
	May be problems for already-escaped input text e.g. "&lt;" -> "&amp;lt;"
	See <https://wiki.python.org/moin/EscapingHtml> for some better(?) ways.
	"""
	return "".join(html_escape_table.get(c,c) for c in text)

#----------------------------------------------------------------------------------------------------

#----------------------------------------------------------------------------------------------------
def dummy_start_response(status, header):
	pass
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
class Session:
	sessions = dict()
	def __init__(self, args):

		id = args.get("session", None)
		if not id:
			while True:
				id = str(uuid.uuid4()).split("-")[0]
				if id not in Session.sessions: break

		self.args = args
		self.args["session"] = id
#----------------------------------------------------------------------------------------------------




#----------------------------------------------------------------------------------------------------
class Counter:
	def __init__(self, start=None, end=None):
		self.hourly = dict()
		self.daily = dict()
		self.monthly = dict()
		self.weekdaily = dict()
		self.lines = 0
		self.requests = 0
		self.errors = 0
		self.weekdays = ["$0Sunday","$1Monday","$2Tuesday","$3Wednesday","$4Thursday","$5Friday","$6Saturday"]

		# preset keys
		if start and end:
			t = seiscomp.core.Time(start)
			while t < end:
				day = t.toString("%Y-%m-%d")
				self.daily.setdefault(day, (0,0,0,0))
				month = t.toString("%Y-%m")
				self.monthly.setdefault(month, (0,0,0,0))
				wd = self.weekdays[int(t.toString("%w"))]
				self.weekdaily.setdefault(wd, (0,0,0,0))
				#
				t += seiscomp.core.TimeSpan(86400)

			for h in range(0,24):
				self.hourly.setdefault("%02d"%int(h), (0,0,0,0))


	def __call__(self, request):
		date = request.created()

		l = request.summary().totalLineCount()
		e = request.summary().totalLineCount() - request.summary().okLineCount()

		volSize = 0
		for i in range(request.arclinkStatusLineCount()):
			sline = request.arclinkStatusLine(i)
			volSize += sline.size()
		volSize /= 1e6

		hour = date.toString("%H")
		(requests, lines, errors, size) = self.hourly.setdefault(hour, (0,0,0,0))
		self.hourly[hour] = (requests+1, lines+l, errors+e, size+volSize)

		day = date.toString("%Y-%m-%d")
		(requests, lines, errors, size)= self.daily.setdefault(day, (0,0,0,0))
		self.daily[day] = (requests+1, lines+l, errors+e, size+volSize)

		month = date.toString("%Y-%m")
		(requests, lines, errors, size) = self.monthly.setdefault(month, (0,0,0,0))
		self.monthly[month] = (requests+1, lines+l, errors+e, size+volSize)

		wd = self.weekdays[int(date.toString("%w"))]
		(requests, lines, errors, size) = self.weekdaily.setdefault(wd, (0,0,0,0))
		self.weekdaily[wd] = (requests+1, lines+l, errors+e, size+volSize)


#----------------------------------------------------------------------------------------------------




#----------------------------------------------------------------------------------------------------
class WebReqLog(seiscomp.client.Application):

	def __init__(self, argc, argv):
		seiscomp.client.Application.__init__(self, argc, argv)
		self.setLoggingToStdErr(True)

		self.setMessagingEnabled(True)
		self.setDatabaseEnabled(True, True)

		self.setAutoApplyNotifierEnabled(False)
		self.setInterpretNotifierEnabled(False)
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def createCommandLineDescription(self):
		seiscomp.client.Application.createCommandLineDescription(self)

		self.commandline().addGroup("WebServer")
		self.commandline().addStringOption("WebServer", "webhost", "serve on IP address; default: all interfaces", "")
		self.commandline().addIntOption("WebServer", "port", "listen on port; default: 8000", 8000)

		self.commandline().addGroup("Export")
		self.commandline().addStringOption("Export", "startTime,b", "start date: YYYY-MM-DD")
		self.commandline().addStringOption("Export", "endTime,e", "end date: YYYY-MM-DD")
		self.commandline().addStringOption("Export", "export", "comma-separated list of: file:xxx-date.html or email:abc@def.de tokens")


		return True
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def validateParameters(self):
		self.urlBase = ""
		self.server = self.commandline().optionString("webhost")
		self.port = self.commandline().optionInt("port")
		print ("Server:", self.server, "Port: ", self.port)

		self.export = []
		if self.commandline().hasOption("export"):
			self.export = self.commandline().optionString("export").split(",")

			try:
				self.startTime = str2date(self.commandline().optionString("startTime"))
				self.endTime = str2date(self.commandline().optionString("endTime"))
			except:
				now = datetime.now()
				start = now - timedelta(days=1)
				tomorrow = now + timedelta(days=0)
				self.startTime = seiscomp.core.Time(start.year, start.month, start.day)
				self.endTime = seiscomp.core.Time(tomorrow.year, tomorrow.month, tomorrow.day)

			if self.server != "":
				self.urlBase = "http://" + self.server + ":" + str(self.port)
			else: self.urlBase = "http://" + socket.gethostname() + ":" + str(self.port)

			print ("Export:", self.export)
			print ("Time range:", self.startTime.iso(), "to", self.endTime.iso())
			print ("Server URL:", self.urlBase)

		return True
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def initConfiguration(self):
		if not seiscomp.client.Application.initConfiguration(self):
                        print ("not seiscomp.client.Application.initConfiguration")
                        return False


		self.urls = {"summary": self.wwwSummary \
					,"requests": self.wwwRequests \
					#,"xml": self.wwwXML \
					,"index": self.wwwIndex \
					,"chart": self.wwwChart \
					,"js": self.wwwloadJS \
					}

		# force logging to stderr even if logging.file = 1
		self.setLoggingToStdErr(True)

		# display human readable byte count / timewindow
		self.bytes = False
		self.secs = False

		self.timeFormat = "%Y-%m-%d %H:%M:%S"
		return True
#----------------------------------------------------------------------------------------------------

#----------------------------------------------------------------------------------------------------
	def run(self):
		if len(self.export) == 0:
			httpd = make_server(self.server, self.port, self.wwwApp)
			print( "Serving on %s port %d ..." % (self.server, self.port))
			httpd.serve_forever()
		else:
			self.exporter()

		return True
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def htmlHeader(self, script=""):
		a = """<html>
<head>
<title>Arclink Request Statistics</title>

<style type="text/css">
<!--
a:link {font-weight:normal; color:blue; text-decoration:none}
a:visited {font-weight:normal; color:blue; text-decoration:none}
a.border{outline:1px solid black; margin:2px; padding:3px; background-color:lightgray; color:black}
span.error{color:red}
span.ok{color:darkgreen}
div.body{width:75%; margin:20px; border: 1px dashed #000; padding:10px; min-width:800px;}
div.RequestHeader{font-size:smaller; background-color: #e9e9e9; -moz-border-radius: 10px; -webkit-border-radius: 10px; border: 1px dashed #000; padding: 10px;}
div.IndexMenu{font-size:smaller; background-color: #e9e9e9; -moz-border-radius: 10px; -webkit-border-radius: 10px; border: 1px dashed #000; padding: 10px;}

table tr:hover { background-color:#eee;}
table.sortable thead { background-color:#eee; color:#666666; font-weight: bold; cursor: pointer;}
table.sortable thead:hover { background-color:#ddd; color:#666666; font-weight: bold; cursor: pointer;}
table.sortable {-moz-border-radius: 10px; -webkit-border-radius: 10px; border: 1px dashed #000; padding: 10px;}
tr.sortbottom {background-color:#eee}
td { text-align:right; }
td.left { text-align:left; }

#f1 { position:absolute; bottom:0px; right:0px; padding:3}
#f2 { position:fixed; top:5px; right:5px; background-color:#afa; outline:2px solid red; font-size:smaller; padding:3 }

body.white {background-color:#ffffff}
body.black {background-color:#afafaf}

span.submit {padding:10px; outline:0px solid gray; margin:10; -moz-border-radius: 20px; background-color:gray}
div.SelectMenu {padding:10px; outline:0px solid gray; margin:10; -moz-border-radius: 20px; background-color:lightgray}
input.Xsubmit {color:black; background-color:lightgreen }

div#mask { display: none; cursor: wait; z-index: 9999; position: absolute; top: 0; left: 0; height: 100%; width: 100%; background-color: #fff; opacity: 0; filter: alpha(opacity = 0);}

-->
</style>
<script type="text/javascript">
function hide(o)
{
	o.style.display="none";
}
</script>
"""

		b = """
</head>
"""
		return a+script+b
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def htmlFooter(self):
		return """
</html>
"""
#----------------------------------------------------------------------------------------------------



#----------------------------------------------------------------------------------------------------
	def exporter(self):
		environ = dict()
		environ["PATH_INFO"] = "/summary"
		environ["QUERY_STRING"] = "startTime="+date2str(self.startTime, 'date')+"&endTime="+date2str(self.endTime, 'date')
		ret = ""
		for i in self.wwwApp(environ, dummy_start_response):
			ret += i

		action = {"eMail":self.sendMail, "file":self.writeFile}

		for i in self.export:
			d = i.split(":")
			if len(d) != 2:
				print( "ERROR: Export action must be one of the following:")
				print( "  (", ", ".join(action.keys()), ")")
				return
			try:
				action[d[0]](d[1], ret)
			except KeyError:
				print ("WARNING: no action found for: %s:%s" % (d[0],d[1]))

#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def sendMail(self, recipient, data):
		print ("sending Mail to: %s via: %s" % (recipient, EMAIL_SMTP))

		try:
			msg = MIMEText(data, 'html')
			msg['Subject'] = "ArcLink Request Log Report"
			msg['From'] = EMAIL_FROM
			msg['To'] = recipient
			text = msg.as_string()
			server = smtplib.SMTP(EMAIL_SMTP)
			server.sendmail(EMAIL_FROM, recipient , text)
			server.quit()
		except (Exception, e):
			print( "ERROR: could not send mail to: %s\n%s" % (recipient,e ))

		return
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def writeFile(self, filename, data):
		print ("writing HTML to file: %s" % filename)

		try:
			file = open(filename, "w")
			file.write(data)
			file.close()
		except:
			print ("ERROR: writing to file %s failed" % filename)

		return
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	# assemble link
	def link(self, page, text, args=None, cls=''):
		if not args: args = dict()
		if len(cls) > 0: cls='class="%s"' % (cls)
		return '<a %s href="%s/%s?%s">%s</a>' % (cls, self.urlBase, page, "&amp;".join(["%s=%s"%(k,v.replace(" ","%20")) for k,v in args.items()]), text)
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def wwwApp(self, environ, start_response):

		d = {}
		for e in re.split(r"[\?|&]", environ.get("QUERY_STRING", "")):
			m = re.search(r"^(.+?)=(.*)$", e)
			if m: d[m.group(1)] = m.group(2).replace("+", " ").replace("%3A", ":")

		environ["myArgs"] = d

		# callback
		return self.urls.get(environ.get("PATH_INFO", "").lstrip("/"), self.wwwIndex)(environ, start_response)
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def wwwloadJS(self, environ, start_response):
		ret = ""
		files = {	"st":"sorttable.js",
					"hc":"highcharts.js",
					"hcIE":"excanvas.compiled.js",
					"dt":"datetimepicker_css.js"
		}

		filename = files.get(environ["myArgs"].get("name", "None"))

		try:
			f = open(filename, "r")
			for l in f:	ret += l
			f.close()
			status = '200 OK'
			headers = [('Content-type', 'text/javascript')]
		except: # FIXME error page is not displayed correctly
			status = '404 ERROR'
			headers = [('Content-type', 'text/html')]
			ret = "<html><body><p><b>ERROR</b>: File not found: %s</p></body></html>" % filename

		start_response(status, headers)
		return [ret]
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def wwwIndex(self, environ, start_response):
		status = '200 OK'
		headers = [('Content-type', 'text/html')]
		start_response(status, headers)
		ret = self.htmlHeader(script='<script src="%s/js?name=dt"></script>' % self.urlBase)

		ret += '<div class="body">'
		ret += '<h1>ArcLink Request Log Tool</h1><br>'

		now = datetime.now()
		start = now - timedelta(days=3)
		tomorrow = now + timedelta(days=1)
		startTime = seiscomp.core.Time(start.year, start.month, start.day)
		endTime = seiscomp.core.Time(tomorrow.year, tomorrow.month, tomorrow.day)

		ret += '<div class="IndexMenu">'
		ret += "<h2>%s</h2>" % "SHOW REQUESTS AND SUMMARY"
		ret += """
		<div class="SelectMenu">
		Please select a time range:
		<form name="input" action="summary" method="get">
		"""
		ret += """
		<a href="javascript:NewCssCal('startTime', 'yyyyMMdd', 'DropDown', false, 24, true)">
		Start Time: </a>
		<input name="startTime" id="startTime" type="text" size="20" value=%s>
		""" % ('"'+date2str(startTime, "date time")+'"')
		ret += """
		<a href="javascript:NewCssCal('endTime', 'yyyyMMdd', 'DropDown', false, 24, true)">
		  End Time: </a>
		<input name="endTime" id="endTime" type="text" size="20" value=%s>
		""" % ('"'+date2str(endTime, "date time")+'"')
		ret += """
		<span class="submit">
		<input type="submit" value="Submit" class="submit" />
		<input type="reset" value="Reset">
		</span>
		</form>
		</div>
		"""
		ret += """
		<div class="SelectMenu">
		Or give a single ArcLink RequestID:
		<form name="requestID" action="requests" method="get">
		RequestID: <input name="requestID" type="text" size="25">
		<span class="submit">
		<input name="lines" type="hidden" value="yes">
		<input type="submit" value="Submit" class="submit">
		</span>
		</form>
		</div>
		"""

		ret += "</div><br>"

		ret += '<div class="IndexMenu">'
		ret += "<h2>%s</h2>" % "SHOW ACCESS STATISTIC CHART"

		now = datetime.now()
		start = now - timedelta(days=6)
		tomorrow = now + timedelta(days=1)
		startTime = seiscomp.core.Time(start.year, start.month, start.day)
		endTime = seiscomp.core.Time(tomorrow.year, tomorrow.month, tomorrow.day)

		ret += """
		<div class="SelectMenu">
		Please select a time range:
		<form name="chart" action="chart" method="get">
		"""
		ret += """
		<a href="javascript:NewCssCal('start', 'yyyyMMdd', 'DropDown', false, 24, true)">
		Start Time: </a>
		<input name="startTime" id="start" type="text" size="20" value=%s>
		""" % ('"'+date2str(startTime, "date time")+'"')
		ret += """
		<a href="javascript:NewCssCal('end', 'yyyyMMdd', 'DropDown', false, 24, true)">
		  End Time: </a>
		<input name="endTime" id="end" type="text" size="20" value=%s>
		""" % ('"'+date2str(endTime, "date time")+'"')

		ret += """<p>
		Select Chart Parameter/Options:<br>
		Plotting:
		<select name="plotting" class="Auswahl" size="1">
			<option value="daily">daily</option>
			<option value="monthly">monthly</option>
			<option value="hourly">hourly</option>
			<option value="weekdaily">weekdaily</option>
		</select>
		</p>
		"""

		ret += """<p>
		Request Type:
		<select name="type" class="Auswahl" size="1">
			<option value="WAVEFORM">Waveform</option>
			<option value="ROUTING">Routing</option>
			<option value="INVENTORY">Inventory</option>
			<option value="RESPONSE">Response</option>
			<option value="Qc">Qc</option>
			<option value="any">any</option>
		</select>
		</p>
		"""

		ret += """<p>
		Parameter 1:
		<select name="parameter1" class="Auswahl" size="1">
			<option value="requests"># of Requests</option>
			<option value="lines"># of Request Lines</option>
			<option value="errors"># of errorneous Lines</option>
			<option value="bytes">transferred Bytes</option>
		</select>
		</p>
		"""

		ret += """<p>
		Net Class:
		<select name="netClass" class="Auswahl" size="1">
			<option value="any">any</option>
			<option value="p">permanent</option>
			<option value="t">temporary</option>
		</select>
		</p>
		"""

		ret += """<p>
		Access:
		<select name="restricted" class="Auswahl" size="1">
			<option value="any">any</option>
			<option value="yes">restricted</option>
			<option value="no">public</option>
		</select>
		</p>
		"""

		ret += """
		<span class=submit>
		<input type="submit" value="Submit"  class="submit"/>
		<input type="reset" value="Reset">
		</span>
		</form>
		</div>
		"""

		ret += "</div>"
		ret += "</div>"
		ret += self.htmlFooter()
		return [ret]
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def wwwChart(self, environ, start_response):
		status = '200 OK'
		headers = [('Content-type', 'text/html')]
		start_response(status, headers)

		# init session
		session = Session(environ["myArgs"])

		startTime = str2date(session.args.get("startTime"))
		endTime = str2date(session.args.get("endTime"))
		counter = Counter(startTime, endTime)

		plotting = session.args.get("plotting", "daily")

		d = map(int, date2str(startTime, 'date').split('-'))

		yLabel = "Count"

		parameter1 = session.args.get("parameter1", "requests")
		if parameter1 == "requests":
			parameter1 = ("RequestCount", 0)
		elif parameter1 == "lines":
			parameter1 = ("LineCount", 1)
		elif parameter1 == "errors":
			parameter1 = ("ErrorCount", 2)
		elif parameter1 == "bytes":
			parameter1 = ("VolumeSize", 3)
			yLabel = "MBytes"

		para, col = parameter1

		series = """
			name: '%s'
		""" % para

		formatter = """
			formatter: function() {
			return '<b>'+ this.series.name +'</b><br/>'+
			this.y +' '+ this.x;
		"""

		catlist = "categories: [" + ",".join(["'%s'" % re.sub(r"\$\d","",i) for i in sorted(eval("counter.%s.keys()" % plotting))]) + "],"
		print (catlist)
		xAxisType = "linear"
		if plotting == "daily":
			xAxisType = "datetime"
			catlist = ''
			formatter = """
				formatter: function() {
				return '<b>'+ (this.point.name || this.series.name) +'</b><br/>'+
				Highcharts.dateFormat('%A %B %e %Y', this.x) + ':<br/>'+
				'' + this.y;
            """
			series = """
				type: 'column',
				name: '%s',
				pointInterval: 24 * 3600 * 1000,
				pointStart: Date.UTC(%d, %d, %02d),
			""" % (para, d[0],d[1]-1,d[2])

		script = """
<script type="text/javascript" src="http://ajax.googleapis.com/ajax/libs/jquery/1.3.2/jquery.min.js"></script>
<script type="text/javascript" src="%s/js?name=hc"></script>
<!--[if IE]>
	<script type="text/javascript" src="%s/js/excanvas.compiled.js"></script>
<![endif]-->
<script type="text/javascript">

var chart;
$(document).ready(function() {
   chart = new Highcharts.Chart({

	credits: {
		enabled:false
	},

   chart: {
		renderTo: 'chartContainer',
		defaultSeriesType: 'column',
		zoomType: 'x'

   },
   title: {
      text: 'ArcLink Request Count'
   },
   xAxis: {
		%s
		type: '%s'
   },
   yAxis: {
      title: {
         text: '%s'
      }
   },
   tooltip: {
		%s
      }
   },
   plotOptions: {
      column: {
         data: '%s',
         // define a custom data parser function for both series
         dataParser: function(data) {
            var table = document.getElementById(data),
               // get the data rows from the tbody element
               rows = table.getElementsByTagName('tbody')[0].getElementsByTagName('tr'),
               // define the array to hold the real data
               result = [],
               // decide which column to use for this series
               column = { '%s': %d}[this.options.name];

            // loop through the rows and get the data depending on the series (this) name
            for (var i = 0; i < rows.length-1; i++) {
				node = rows[i].getElementsByTagName('td')[column];

				if (node.getAttribute("rawdata") != null) {
					value = node.getAttribute("rawdata");
				}
				else {
					value = node.innerHTML;
				}
               result.push(parseInt(value));
            }
            return result;
         }
      }
   },
   series: [{%s}]
});});


</script>
		""" % (self.urlBase, self.urlBase, catlist, xAxisType, yLabel, formatter, plotting, para,col, series)


		script += '<script src="%s/js?name=st"></script>' % self.urlBase

		ret = self.htmlHeader(script)
		out = StringIO()
		self.printArgs(out, session.args)

		try:
			self.loadRequests(session, counter)
		except Exception:
			logs.print_exc()
			return ["Ooooops. Please look at the log files....."]




		ret += '<div class="body">'
		ret += '<div id="chartContainer" style="width: 800px; height: 400px; margin: 0 auto"></div>'
		ret += "<br><hr><br>"

		if plotting == "hourly":
			ret += '<table id="hourly" class="datatable sortable" width=60%><thead><tr><td>Hour</td><td>RequestCount</td><td>LineCount</td><td>ErrorCount</td><td>VolumeSize</td></tr></thead>'
			ret += "Hourly Count (%s - %s)<tbody>" % (date2str(startTime, 'date'), date2str(endTime, 'date'))
			tr = tl = te = ts = 0
			for k,(r,l,e,s) in sorted(counter.hourly.items()):
				ret += '<tr><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%f">%8.2f Mb</td></tr>' % (k,r,l,e,s,s)
				tr += r
				tl += l
				te += e
				ts += s
			ret += '<tr class="sortbottom"><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td>%8.2f Mb</td></tr>' % ("Total",tr,tl,te,ts)
			ret += "</tbody></table>"

		elif plotting == "daily":
			ret += '<br><table id="daily" class="datatable sortable" width=60%><thead><tr><td>Day</td><td>RequestCount</td><td>LineCount</td><td>ErrorCount</td><td>VolumeSize</td></tr></thead>'
			ret += "Daily Count<tbody>"
			tr = tl = te = ts = 0
			for k,(r,l,e,s) in sorted(counter.daily.items()):
				ret += '<tr><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%f">%8.2f Mb</td></tr>' % (k,r,l,e,s,s)
				tr += r
				tl += l
				te += e
				ts += s
			ret += '<tr class="sortbottom"><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td>%8.2f Mb</td></tr>' % ("Total",tr,tl,te,ts)
			ret += "</tbody></table>"

		elif plotting == "weekdaily":
			ret += '<br><table id="weekdaily" class="datatable sortable" width=60%><thead><tr><td>WeekDay</td><td>RequestCount</td><td>LineCount</td><td>ErrorCount</td><td>VolumeSize</td></tr></thead>'
			ret += "WeekDaily Count (%s - %s)<tbody>" % (date2str(startTime, 'date'), date2str(endTime, 'date'))
			tr = tl = te = ts = 0
			for k,(r,l,e,s) in sorted(counter.weekdaily.items()):
				ret += '<tr><th sorttable_customkey="%s" align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%f">%8.2f Mb</td></tr>' % (k,k[2:],r,l,e,s,s)
				tr += r
				tl += l
				te += e
				ts += s
			ret += '<tr class="sortbottom"><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td>%8.2f Mb</td></tr>' % ("Total",tr,tl,te,ts)
			ret += "</tbody></table>"

		elif plotting == "monthly":
			ret += '<br><table id="monthly" class="datatable sortable" width=60%><thead><tr><td>Month</td><td>RequestCount</td><td>LineCount</td><td>ErrorCount</td><td>VolumeSize</td></tr></thead>'
			ret += "Monthly Count<tbody>"
			tr = tl = te = ts = 0
			for k,(r,l,e,s) in sorted(counter.monthly.items()):
				ret += '<tr><th align="left">%s</th><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%f">%8.2f Mb</td></tr>' % (k,r,l,e,s,s)
				tr += r
				tl += l
				te += e
				ts += s
			ret += '<tr class="sortbottom"><th align="left">%s</th><td align="right">%s</td><td align="right">%s</td><td align="right">%s</td><td align="right">%8.2f Mb</td></tr>' % ("Total",tr,tl,te,ts)
			ret += "</tbody></table>"

		ret += "<br>"

		ret += "</div>"

		ret += out.getvalue()
		out.close()
		ret += self.htmlFooter()
		return [ret]
#----------------------------------------------------------------------------------------------------




#----------------------------------------------------------------------------------------------------
	def wwwSummary(self, environ, start_response):
		status = '200 OK'
		headers = [('Content-type', 'text/html')]
		start_response(status, headers)

		out = StringIO()
		ret = self.htmlHeader(script='<script src="%s/js?name=st" type="text/javascript"></script>' % self.urlBase)
		ret += '<body><div class="body">\n'

		# init session
		session = Session(environ["myArgs"])

		try:
			requests = self.loadRequests(session)
		except Exception:
			logs.print_exc()
			return ["Ooooops. Please look at the log files....."]

		self.printRequestSummary(out, session, requests)

		while len(requests) > 0:
			request = requests.pop()
			while request.arclinkStatusLineCount() > 0:
				line = request.arclinkStatusLine(0)
				request.remove(line)
				del(line)
			while request.arclinkRequestLineCount() > 0:
				line = request.arclinkRequestLine(0)
				request.remove(line)
				del(line)
			del(request)
		del(requests)

		gc.collect()

		ret += out.getvalue()
		ret += '</div></body>'
		ret += self.htmlFooter()

		out.close()
		return [ret]
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def wwwRequests(self, environ, start_response):
		status = '200 OK'
		headers = [('Content-type', 'text/html')]
		start_response(status, headers)

		out = StringIO()
		ret = self.htmlHeader()
		ret += '<body><div class="body">'

		# init session
		session = Session(environ["myArgs"])

		try:
			requests = self.loadRequests(session)
		except Exception:
			logs.print_exc()
			return ["Ooooops. Please look at the log files....."]

		self.printRequests(out, session, requests)
		del(requests)

		ret += out.getvalue()
		ret += '</div></body>'
		ret += self.htmlFooter()

		out.close()
		return [ret]
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def printRequestSummary(self, out, session, requests):
		print ("generating request summary ...")

		users = dict()
		rtypes = dict()
		streams = dict()
		errors = dict()
		messages = dict()
		total = 0
		volumes = dict()
		userIPs = dict()
		clientIPs = dict()
		nets = dict()

		startTime = str2date(session.args.get("startTime"))
		endTime = str2date(session.args.get("endTime"))
		timeMin = endTime
		timeMax = startTime

		for request in requests:
			user = request.userID()
			netcounts = {}
			volume_error = {}

			if request.arclinkStatusLineCount() == 0:
				self.query().loadArclinkStatusLines(request)
			for i in range(request.arclinkStatusLineCount()):
				sline = request.arclinkStatusLine(i)
				if sline.status() == 'OK': err = 0
				else: err = 1
				volume = sline.volumeID()

				if sline.status() == 'ERROR':
					volume_error[volume] = True
					size = 0

				else:
					volume_error[volume] = False
					size = sline.size()

				(count, error, s) = volumes.setdefault(volume, (0, 0, 0))
				volumes[volume] = (count+1, error+err, s+size)
				(count, lines, error, s) = users.setdefault(user, (0, 0, 0, 0))
				users[user] = (count, lines, error, s+size)
				message = sline.message()
				messages.setdefault(message, 0)
				messages[message] +=1

			if self.export and request.type().upper() == 'WAVEFORM' and request.arclinkRequestLineCount() == 0:
				self.query().loadArclinkRequestLines(request)
			for i in range(request.arclinkRequestLineCount()):
				line = request.arclinkRequestLine(i)

				stream = line.streamID().networkCode()+"."+line.streamID().stationCode()
				message = line.status().message()
				timeWindow = (line.end() - line.start()).seconds()
				if timeWindow < 0: timeWindow = 0

				if line.status().status() == 'ERROR':
					error = 1
					nodata = 0
					size = 0

				elif line.status().status() == 'NODATA':
					error = 0
					nodata = 1
					size = 0

				else:
					error = 0
					nodata = 0

					try:
						if volume_error[line.status().volumeID()]:
							size = 0
						else:
							size = line.status().size()

					except KeyError: # should not happen
						size = line.status().size()

				(lines, nodataCount, errorCount, s, tw) = streams.setdefault(stream, (0, 0, 0, 0, 0))
				streams[stream] = (lines+1, nodataCount+nodata, errorCount+error, s+size, tw+timeWindow)

				messages.setdefault(message, 0)
				messages[message] +=1

				netcode = line.streamID().networkCode()
				if netcode[0] in 'XYZ':
					netcode += '/' + line.start().toString('%Y')

				(lines, nodataCount, errorCount, s) = netcounts.setdefault(netcode, (0, 0, 0, 0))
				netcounts[netcode] = (lines+1, nodataCount+nodata, errorCount+error, s+size)

			for netcode, (lineCount, nodataCount, errorCount, size) in netcounts.items():
				(count, lines, nodata, error, s) = nets.setdefault(netcode, (0, 0, 0, 0, 0))
				nets[netcode] = (count +1, lines+lineCount, nodata+nodataCount, error+errorCount, s+size)

			lineCount = request.summary().totalLineCount()
			errorCount = request.summary().totalLineCount() - request.summary().okLineCount()

			(count, lines, error, size) = users.setdefault(user, (0, 0, 0, 0))
			users[user] = (count +1, lines+lineCount, error+errorCount, size)

			userIP = request.userIP()
			if len(userIP) == 0: userIP = "unknown"
			(count, lines, error) = userIPs.setdefault(userIP, (0, 0, 0))
			userIPs[userIP] = (count +1, lines+lineCount, error+errorCount)

			clientIP = request.clientIP()
			if len(clientIP) == 0: clientIP = "unknown"
			(count, lines, error) = clientIPs.setdefault(clientIP, (0, 0, 0))
			clientIPs[clientIP] = (count +1, lines+lineCount, error+errorCount)

			rtype = request.type()
			(count, lines, error) = rtypes.setdefault(rtype, (0, 0, 0))
			rtypes[rtype] = (count +1, lines+lineCount, error+errorCount)

			if errorCount > 0:
				rid = request.requestID()
				errors.setdefault(rid, 0)
				errors[rid] += (errorCount)

			total += lineCount

			if request.created() < timeMin: timeMin = request.created()
			if request.created() > timeMax: timeMax = request.created()

		args = dict()
		out.write("%s\n" % self.link("", "Start Page", args, cls="border"))

		args = dict()
		args["startTime"] = session.args.get("startTime")
		args["endTime"] = session.args.get("endTime")
		out.write("%s\n" % self.link("summary", "RESET", args, cls="border"))

		args = dict()
		args["startTime"] = (datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d")
		args["endTime"] = (datetime.now()+timedelta(days=1)).strftime("%Y-%m-%d")
		out.write( "%s\n" % self.link("summary", "recent", args, cls="border"))

		args = dict(session.args)
		if session.args.get("lines", "") != "yes":
			args["lines"] = "yes"
			out.write( "%s\n" % self.link("summary", "SHOW detailed station counts", args, cls="border"))
		else:
			del(args["lines"])
			out.write( "%s\n" % self.link("summary", "HIDE detailed station counts", args, cls="border"))

		args = dict(session.args)
		if session.args.get("onlyErrors", "") != "yes":
			args["onlyErrors"] = "yes"
			out.write( "%s\n" % self.link("summary", "SHOW only Requests WITH Errors", args, cls="border"))
		else:
			del(args["onlyErrors"])
			out.write( "%s\n" % self.link("summary", "SHOW Requests WITH & WITHOUT Errors", args, cls="border"))

		self.printArgs(out, session.args)
		out.write("<pre>\n")
		out.write("<h2>Arclink Request Report</h2>\n")
		out.write("generated: %s UTC\n" % datetime.now())
		out.write("\n")
		out.write("Start: %s (first: %s)\n" % (date2str(startTime), date2str(timeMin)))
		out.write("End  : %s (last : %s)\n" % (date2str(endTime), date2str(timeMax)))
		out.write("\n")

		args = dict(session.args)
		args["lines"] = "yes"
		l = self.link("requests", "%d"%len(requests), args)
		out.write(  "Requests:\t%s\n" % l)

		args = dict(session.args)
		args["lines"] = "yes"
		args["onlyErrors"] = "yes"
		l = self.link("requests", "%d"%len(errors), args)
		out.write("  with Errors:\t%s\n" % l)
		out.write("Error Count:\t%d\n" %  sum(errors.values()))
		out.write("Users:\t\t%d\n" % len(users))
		out.write("Total Lines:\t%d\n" % sum([l for c,l,e,s in users.values()]))
		out.write("Total Size:\t%s\n" % byte2h(sum([s for c,e,s in volumes.values()]), self.bytes))

		if len(streams): out.write(  "Stations:\t%d\n" % len(streams))

		out.write("\n")
		out.write('<table class="sortable" width="100%">\n')
		out.write("<thead>\n")
		out.write("<tr><th>User</th><th>Requests</th><th>Lines</th><th>Nodata/Errors</th><th>Size</th></tr>\n")
		out.write("</thead><tbody>\n")
		for k,(r,l,o,s) in sorted(users.items()):
			args = dict(session.args)
			args["userID"] = k
			ul = self.link("summary", "%-32s"%k, args)
			rl = self.link("requests", "%-6d"%r, args)
			args["lines"] = "yes"
			ll = self.link("requests", "%-6d"%l, args)
			args["onlyErrors"] = "yes"
			lo = self.link("requests", "%-6d"%o, args)
			out.write('<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%d">%10s</td></tr>\n' % (ul,rl,ll,lo, s, byte2h(s, self.bytes)))
		out.write("</tbody></table>\n")

		if len(nets) > 0:
			out.write("\n")
			out.write('<table class="sortable" width="100%">\n')
			out.write("<thead>\n")
			out.write("<tr><th>Network</th><th>Requests</th><th>Lines</th><th>Nodata</th><th>Errors</th><th>Size</th></tr>\n")
			out.write("</thead><tbody>\n")
			for k,(r,l,n,o,s) in sorted(nets.items()):
				if s > 0:
					out.write('<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%d">%10s</td></tr>\n' % (k,r,l,n,o, s, byte2h(s, self.bytes)))
			out.write(  "</tbody></table>\n")

		out.write("\n")
		out.write(  '<table class="sortable" width="100%">\n')
		out.write(  "<thead>\n")
		out.write(  "<tr><th>Request Type</th><th>Requests</th><th>Lines</th><th>Nodata/Errors</th></tr>\n")
		out.write(  "</thead><tbody>\n")
		for k,(r,l,o) in rtypes.items():
			args = dict(session.args)
			args["type"] = k
			ul = self.link("summary", "%-32s"%k, args)
			rl = self.link("requests", "%-6d"%r, args)
			args["lines"] = "yes"
			ll = self.link("requests", "%-6d"%l, args)
			args["onlyErrors"] = "yes"
			lo = self.link("requests", "%-6d"%o, args)
			out.write(  '<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n' % (ul,rl,ll,lo))
		out.write(  "</tbody></table>\n")

		out.write( "\n")
		out.write(  '<table class="sortable" width="100%">\n')
		out.write(  "<thead>\n")
		out.write(  "<tr><th>Volume</th><th>Count</th><th>Nodata/Errors</th><th>Size</th></tr>\n")
		out.write(  "</thead><tbody>\n")
		for k,(c,e,s) in sorted(volumes.items()):
			args = dict(session.args)
			args["volume"] = k
			kl = self.link("summary", "%-15s"%k, args)
			args["onlyErrors"] = "yes"
			el = self.link("requests", "%d"%e, args)
			out.write('<tr><td class="left">%s</td><td>%d</td><td>%s</td><td sorttable_customkey="%d">%s</td></tr>\n' % (kl,c,el,s,byte2h(s, self.bytes)))
		out.write("</tbody></table>\n")

		if session.args.get("lines") and len(streams) > 0:
			out.write("\n")
			out.write('<table class="sortable" width="100%">\n')
			out.write("<thead>\n")
			out.write("<tr><th>Station</th><th>Requests</th><th>Nodata</th><th>Errors</th><th>Size</th><th>Time</th></tr>\n")
			out.write("</thead><tbody>\n")
			for k,(r,n,o,s,tw) in sorted(streams.items()):
				args = dict(session.args)
				args["streamID"] = k+".*.*"
				sl = self.link("summary", "%-15s"%k, args)
				args["lines"] = "yes"
				rl = self.link("requests", "%-6d"%r, args)
				args["onlyErrors"] = "yes"
				nl = self.link("requests", "%-6d"%o, args)
				ol = self.link("requests", "%-6d"%o, args)
				out.write('<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td><td sorttable_customkey="%d">%s</td><td sorttable_customkey="%d" >%s</td></tr>\n' % (sl,rl,nl,ol, s,byte2h(s, self.bytes), tw,sec2h(tw, self.secs)))
			out.write("</tbody></table>\n")

		if len(messages) > 1 or (len(messages) == 1 and list(messages.keys())[0] != ""):
			out.write("\n")
			out.write('<table class="sortable" width="100%">\n')
			out.write("<thead>\n")
			out.write("<tr><th>Count</th><th>Message</th></tr>\n")
			out.write("</thead><tbody>\n")
			for k,c in sorted(messages.items()):
				if len(k) > 0:
					args = dict(session.args)
					args["message"] = html_escape(k).replace(" ", "%20")
					kl = self.link("requests", "%-30s" % html_escape(k), args)
					out.write('<tr><td class="left">%d</td><td>%s</td></tr>\n' % (c,kl))
			out.write("</tbody></table>\n")

		if len(userIPs) > 0:
			out.write("\n")
			out.write('<table class="sortable" width="100%">\n')
			out.write("<thead>\n")
			out.write("<tr><th>UserIP</th><th>Requests</th><th>Lines</th><th>Nodata/Errors</th></tr>\n")
			out.write("</thead><tbody>\n")
			for k,(r,l,o) in sorted(userIPs.items()):
				args = dict(session.args)
				args["userIP"] = k
				sk = self.link("summary", "%s"%k, args)
				rl = self.link("requests", "%-6d"%r, args)
				args["lines"] = "yes"
				ll = self.link("requests", "%-6d"%l, args)
				args["onlyErrors"] = "yes"
				lo = self.link("requests", "%-6d"%o, args)
				out.write('<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n' % (sk,rl,ll,lo))
			out.write("</tbody></table>\n")

		if len(clientIPs) > 0:
			out.write("\n")
			out.write('<table class="sortable" width="100%">\n')
			out.write("<thead>\n")
			out.write("<tr><th>ClientIP</th><th>Requests</th><th>Lines</th><th>Nodata/Errors</th></tr>\n")
			out.write("</thead><tbody>\n")
			for k,(r,l,o) in sorted(clientIPs.items()):
				args = dict(session.args)
				args["clientIP"] = k
				sk = self.link("summary", "%s"%k, args)
				rl = self.link("requests", "%-6d"%r, args)
				args["lines"] = "yes"
				ll = self.link("requests", "%-6d"%l, args)
				args["onlyErrors"] = "yes"
				lo = self.link("requests", "%-6d"%o, args)
				out.write('<tr><td class="left">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n' % (sk,rl,ll,lo))
			out.write( "</tbody></table>\n")

		out.write("</pre>\n")
		out.write("<hr /><address>\n")
		args = list()
		for w in sys.argv:
			if w.startswith("mysql:"):
				# Suppress connection details.
				tmp = w.split("/")
				tmp[2] = "[HIDDEN]"
				w = "/".join(tmp)
			args.append(w)
		osuser = os.getenv("LOGNAME").replace("LOGNAME=","")
		out.write(osuser + "@" + socket.getfqdn() + " " + " ".join(args)+"\n")
		out.write("<br />Version:" + VERSION)
		out.write("</address>")


#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def printRequests(self, out, session, requests):
		self.printArgs(out, session.args)
		args = dict(session.args)
		if session.args.has_key("lines"):
			del(args["lines"])
		out.write("%s\n" % self.link("summary", "Summary Page", args, cls="border"))

		out.write( "<pre><br>\n")
		out.write( "<h2>Arclink Requests</h2>\n")

		if session.args.get("onlyErrors", None):
			out.write( "only erroneous requests lines are displayed\n")

		streamID = session.args.get("streamID", None)
		if streamID:
			out.write( "limited to station: %s\n" % streamID)

		message = session.args.get("message", None)
		if message:
			out.write( "limited to message: %s\n" % message.replace("%20", " "))
		out.write("\n")


		for request in requests:
			out.write('<div class="RequestHeader">\n')

			args = dict()
			args["session"] = session.args.get("session")
			args["requestID"] = request.requestID()
			args["lines"] = "yes"
			rl = self.link("requests", "%s"%request.requestID(), args)
			out.write("REQUEST_ID %s" % rl)

			out.write("TYPE %s" % request.type())
			out.write("USER %s" % request.userID())
			if request.userIP(): out.write("USER_IP %s \n"% request.userIP())
			out.write("CREATED %s \n" % request.created())
			if request.clientID(): out.write("CLIENT_ID %s \n" % request.clientID())
			if request.clientIP(): out.write("CLIENT_IP %s \n" % request.clientIP())
			if request.header(): out.write("HEADER %s\n" % request.header())
			if request.label(): out.write("LABEL %s\n" % request.label())

			for i in range(request.arclinkRequestLineCount()):
				line = request.arclinkRequestLine(i)


				if line.status().status() == "OK": cl = "ok"
				else: cl = "error"

				out.write('<span class="%s">%s' % (cl , line.start()), line.end(), \
						line.streamID().networkCode()+ \
						" "+line.streamID().stationCode()+ \
						" "+line.streamID().locationCode()+ \
						" "+line.streamID().channelCode(), \
						"("+line.constraints()+")", \
						line.status().volumeID(), \
						line.status().status(), \
						line.status().size(), \
						line.status().message(), \
						"</span>\n")
			try: reqErrors = request.summary().totalLineCount() - request.summary().okLineCount()
			except: reqErrors = 0
			if reqErrors > 0: cl = "error"
			else: cl = "ok"
			try:
				out.write('<span class="%s">TOTAL_LINES %d\n' % (cl, request.summary().totalLineCount()))
				out.write("ERROR_LINES %d</span>\n" % reqErrors)
				out.write("AVERAGE_TIME_WINDOW %d\n" % request.summary().averageTimeWindow())
			except:
				out.write("!!! ERROR in printRequests() !!!\n")
				pass

			if request.arclinkStatusLineCount() == 0:
				self.query().loadArclinkStatusLines(request)
			for i in range(request.arclinkStatusLineCount()):
				line = request.arclinkStatusLine(i)
				if line.status() != 'OK': cl = "error"
				else: cl = "ok"
				out.write('VOLUME %s <span class="%s">%s</span> %d <span class="%s">%s</span>\n' % (line.volumeID(), cl, line.status(), line.size(), cl, line.message()))

			out.write(request.status(), request.message())
			out.write("</div>\n")

		out.write( "</pre>\n")

#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def loadRequests(self, session, cb=None):
		print( "loading requests ...")

		startTime = str2date(session.args.get("startTime", "2020-04-01"))
		endTime = str2date(session.args.get("endTime", "2020-04-02"))
		userID = session.args.get("userID", "%").replace("*", "%").replace("?", "%")
		type = session.args.get("type", "%")
		#
		netClass = session.args.get("netClass", "%")
		if netClass == "any": netClass = "%"
		#
		restricted = session.args.get("restricted", None)
		if restricted == "no": restricted = "0"
		elif restricted == "yes": restricted = "1"
		else: restricted = None
		#
		requestID = session.args.get("requestID", None)
		#
		onlyErrors = session.args.get("onlyErrors", False)
		lines = session.args.get("lines", False)
		volume = session.args.get("volume", False)
		message = session.args.get("message", False)
		userIP = session.args.get("userIP", False)
		clientIP = session.args.get("clientIP", False)

		streamID = session.args.get("streamID", "%.%.%.%").replace("*", "%").replace("?", "%")
		try:
			(net, sta, loc, cha) = streamID.split(".")
		except:
			(net, sta, loc, cha) = ("%","%","%","%")

		if requestID:
			query = self.query().getArclinkRequestByRequestID(requestID)
		elif session.args.get("streamID", None) or session.args.get("netClass", None):
			query = self.query().getArclinkRequest(userID, startTime, endTime, net, sta, loc, cha, type, netClass)
		else:
			query = self.query().getArclinkRequestByUserID(userID, startTime, endTime, type)

		requests = list()
		for obj in query:
			request = seiscomp.datamodel.ArclinkRequest.Cast(obj)

			# ignore unfinished (fdsnws) requests
			try:
				request.summary()
			except:
				continue

			if request:
				requests.append(request)

		if not (restricted or onlyErrors or lines or volume or message or userIP or clientIP or cb):
			return requests


		print("using selection ...")

		streamID = session.args.get("streamID", "").replace("*", "").replace("?", "")
		try:
			(net, sta, loc, cha) = streamID.split(".")
		except:
			(net, sta, loc, cha) = ("","","","")

		if restricted == "0": restricted = False
		if restricted == "1": restricted = True

		if userIP == "unknown": userIP = ""
		if clientIP == "unknown": clientIP = ""
		selection = list()
		for request in requests:
			try: reqErrors = request.summary().totalLineCount() - request.summary().okLineCount()
			except: reqErrors = 0

			foundUserIP = False
			foundClientIP = False
			if userIP != False:
				if userIP == request.userIP(): foundUserIP = True
				else: continue
			if clientIP != False:
				if clientIP == request.clientIP(): foundClientIP = True
				else: continue

			if onlyErrors and request.status() != 'END': foundRequest = True
			else: foundRequest = False

			Xrequest = seiscomp.datamodel.ArclinkRequest(request)

			foundVolume = False
			self.query().loadArclinkStatusLines(request)
			if volume and request.arclinkStatusLineCount() == 0: continue
			for i in range(request.arclinkStatusLineCount()):
				sline = request.arclinkStatusLine(i)
				vol = sline.volumeID()
				if volume and volume != vol: continue
				if message and message.replace("%20", " ") != sline.message(): continue
				status = sline.status()
				if onlyErrors and status == 'OK': continue
				Xrequest.add(DataModel.ArclinkStatusLine(sline))
				foundVolume = True

			foundLine = False
			if lines or restricted is not None:
				self.query().loadArclinkRequestLines(request)
				lineCount = 0
				errorCount = 0
				tw = seiscomp.core.TimeSpan(0.)
				for i in range(request.arclinkRequestLineCount()):
					line = request.arclinkRequestLine(i)
					vol = line.status().volumeID()
					if volume and volume != vol: continue
					if message and message.replace("%20", " ") != line.status().message(): continue
					status = line.status().status()
					if onlyErrors and status == 'OK': continue
					if len(streamID) > 0:
						if net != "" and net != line.streamID().networkCode(): continue
						if sta != "" and sta != line.streamID().stationCode(): continue
						if loc != "" and loc != line.streamID().locationCode(): continue
						if cha != "" and cha != line.streamID().channelCode(): continue
					if restricted is not None and restricted != line.restricted(): continue

					Xrequest.add(DataModel.ArclinkRequestLine(line))
					foundLine = True
					lineCount += 1
					if status != 'OK': errorCount += 1
					start = line.start()
					end = line.end()
					if start <= end:
						tw += end - start

				summary = DataModel.ArclinkRequestSummary()
				summary.setOkLineCount(lineCount - errorCount)
				summary.setTotalLineCount(lineCount)
				averageTimeWindow = tw.seconds()
				if lineCount > 0:
					averageTimeWindow = tw.seconds() / lineCount
				if averageTimeWindow > 2**32: averageTimeWindow = -1
				summary.setAverageTimeWindow(averageTimeWindow)
				Xrequest.setSummary(summary)

			if not lines and not volume and onlyErrors and reqErrors > 0: foundRequest = True
			if not message and not lines and not onlyErrors and not volume and (foundUserIP or foundClientIP): foundRequest = True
			if foundRequest or foundLine or foundVolume:
				if cb: cb(Xrequest)
				else: selection.append(Xrequest)

		if not cb: return selection
#----------------------------------------------------------------------------------------------------


#----------------------------------------------------------------------------------------------------
	def printArgs(self, out, args):
		print('<div id="f2" onclick="hide(this)">',file=out)
		print('<span style="font-weight:bold">Effective Constraints</span><pre>',file=out)
		for k,v in args.items():
			print ( "%s = %s" % (k,v.replace("%20"," ")),file=out)
		print("</pre></div>",file=out)
#----------------------------------------------------------------------------------------------------



#----------------------------------------------------------------------------------------------------
#----------------------------------------------------------------------------------------------------
logs.debug = seiscomp.logging.debug
logs.info = seiscomp.logging.info
logs.notice = seiscomp.logging.notice
logs.warning = seiscomp.logging.warning
logs.error = seiscomp.logging.error
app = WebReqLog(len(sys.argv), sys.argv)
sys.exit(app())
#----------------------------------------------------------------------------------------------------
#----------------------------------------------------------------------------------------------------

