#!/bin/sh
# Nikolaos Triantafyllis (NOA)

# The script will can run as a cron job to report daily with the statistics
# paths and variables need to change accordingly

# 34 05 * * * /home/sysop/scripts/webreqlog_email.sh


# it sends two mails: (i) statistics to "recipients" and (ii) success/failure info of the first e-mail shipping to node maintainer
export USER=sysop
eval `seiscomp print env | head -n -1`

recipients="eida_log@gfz-potsdam.de" # or a list, comma seperated
maintainer="eida@node.org"

cd /home/sysop/seiscomp/lib/python/webreqlog &&
tmpfile=$(mktemp /tmp/webreqlog_report.XXXXXX) &&
tmpfile2=$(mktemp /tmp/webreqlog_report2.XXXXXX) &&
python webreqlog.py -d mysql://sysop:sysop@localhost/seiscomp3 --host localhost --port 8000 --debug --export file:$tmpfile2 2> $tmpfile && 
mail -a "Content-type: text/html" -s "ArcLink Request Log Report" $recipients<$tmpfile2 && mail -s "EIDA webreqlog report" $maintainer<$tmpfile && 
rm $tmpfile $tmpfile2
