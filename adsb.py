import socket
import datetime
import argparse
import time
import psycopg2
import sys
import os
import config
from math import pow
from haversine import haversine, Unit

#ADS-B Settings
HOST = "10.0.0.229"
PORT = 30003
BUFFER_SIZE = 1024
BATCH_SIZE = 1
CONNECT_ATTEMPT_LIMIT = 10
CONNECT_ATTEMPT_DELAY = 5.0

#PostgreSQL Settings
host = '127.0.0.1'
port = '5432'
dbname = 'aircraft'
user = config.username
password = config.password

#Hex Settings
ICAO_SIZE = 6           # size of an icao address
NNUMBER_MAX_SIZE = 6    # max size of a N-Number

charset = "ABCDEFGHJKLMNPQRSTUVWXYZ" # alphabet without I and O
digitset = "0123456789"
hexset = "0123456789ABCDEF"
allchars = charset+digitset

suffix_size = 1 + len(charset) + int(pow(len(charset),2))   # 601
bucket4_size = 1 + len(charset) + len(digitset)             # 35
bucket3_size = len(digitset)*bucket4_size + suffix_size     # 951
bucket2_size = len(digitset)*(bucket3_size) + suffix_size   # 10111
bucket1_size = len(digitset)*(bucket2_size) + suffix_size   # 101711

directory = os.getcwd()

def main():

    #set up command line options
    parser = argparse.ArgumentParser(description="A program to process dump1090 messages then insert them into a database")
    parser.add_argument("-l", "--location", type=str, default=HOST, help="This is the network location of your dump1090 broadcast. Defaults to %s" % (HOST))
    parser.add_argument("-p", "--port", type=int, default=PORT, help="The port broadcasting in SBS-1 BaseStation format. Defaults to %s" % (PORT))
    parser.add_argument("--buffer-size", type=int, default=BUFFER_SIZE, help="An integer of the number of bytes to read at a time from the stream. Defaults to %s" % (BUFFER_SIZE))
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="An integer of the number of rows to write to the database at a time. If you turn off WAL mode, a lower number makes it more likely that your database will be locked when you try to query it. Defaults to %s" % (BATCH_SIZE))
    parser.add_argument("--connect-attempt-limit", type=int, default=CONNECT_ATTEMPT_LIMIT, help="An integer of the number of times to try (and fail) to connect to the dump1090 broadcast before qutting. Defaults to %s" % (CONNECT_ATTEMPT_LIMIT))
    parser.add_argument("--connect-attempt-delay", type=float, default=CONNECT_ATTEMPT_DELAY, help="The number of seconds to wait after a failed connection attempt before trying again. Defaults to %s" % (CONNECT_ATTEMPT_DELAY))

    # parse command line options
    args = parser.parse_args()

    # print args.accumulate(args.in)
    count_since_commit = 0
    count_total = 0
    count_failed_connection_attempts = 1

    # connect to database or create if it doesn't exist
    conn = psycopg2.connect(host = host, database = dbname, user = user, password = password)
    cur = conn.cursor()
    #cur.execute('PRAGMA journal_mode=wal')

    # set up the table if neccassary
    cur.execute("""CREATE TABLE IF NOT EXISTS
        squitters(
            message_type TEXT,
            transmission_type INT,
            session_id INT,
            aircraft_id INT,
            hex_ident TEXT,
            n_num TEXT,
            flight_id SMALLINT,
            generated_date DATE,
            generated_time TIME,
            logged_date DATE,
            logged_time TIME,
            callsign TEXT,
            altitude INT,
            ground_speed REAL,
            track REAL,
            lat FLOAT8,
            lon FLOAT8,
            vertical_rate SMALLINT,
            distance_nm FLOAT8,
            distance_miles FLOAT8,
            squawk SMALLINT,
            alert SMALLINT,
            emergency SMALLINT,
            spi SMALLINT,
            is_on_ground SMALLINT,
            parsed_time TIMESTAMP
        );
    """)

    cur.execute("""CREATE TABLE IF NOT EXISTS
        flight_log(
            log_id SERIAL PRIMARY KEY,
            n_num_log TEXT,
            pilot_name TEXT,
            start_time TIMESTAMP,
            end_time TIMESTAMP
        );
    """)

    cur.execute("""
            CREATE OR REPLACE VIEW callsigns AS
              SELECT callsign, hex_ident, n_num, date(parsed_time) date_seen, max(parsed_time) last_seen, min(parsed_time) first_seen
                FROM squitters
                WHERE callsign != ''
                GROUP BY callsign, hex_ident, n_num, date_seen;
    """)

    cur.execute("""
            CREATE OR REPLACE VIEW locations AS
              SELECT hex_ident, n_num, parsed_time, lon, lat, altitude
                FROM squitters WHERE lat >= 0;
    """)

    cur.execute("""
            CREATE OR REPLACE VIEW log_view AS
              SELECT hex_ident, n_num, altitude, ground_speed, track, lat, lon, vertical_rate, squawk, emergency, spi, is_on_ground, generated_date, generated_time, parsed_time
                FROM squitters
                INNER JOIN flight_log
                ON squitters.parsed_time > flight_log.start_time AND squitters.parsed_time < flight_log.end_time AND squitters.n_num = flight_log.n_num_log
                ORDER BY parsed_time;
    """)


    # open a socket connection
    while count_failed_connection_attempts < args.connect_attempt_limit:
        try:
            s = connect_to_socket(args.location, args.port)
            count_failed_connection_attempts = 1
            print("Connected to dump1090 broadcast")
            break
        except socket.error:
            count_failed_connection_attempts += 1
            print("Cannot connect to dump1090 broadcast. Making attempt %s." % (count_failed_connection_attempts))
            time.sleep(args.connect_attempt_delay)
    else:
        quit()

    data_str = ""

    try:
        #loop until an exception
        while True:
            #get current time
            cur_time = datetime.datetime.utcnow()
            ds = cur_time.isoformat()
            ts = cur_time.strftime("%H:%M:%S")

            # receive a stream message
            try:
                message = ""
                message = s.recv(args.buffer_size)
                message = message.decode('utf-8')
                data_str = message.strip("\n")
            except socket.error:
                # this happens if there is no connection and is delt with below
                pass

            if len(message) == 0:
                print(ts, "No broadcast received. Attempting to reconnect")
                time.sleep(args.connect_attempt_delay)
                s.close()

                while count_failed_connection_attempts < args.connect_attempt_limit:
                    try:
                        s = connect_to_socket(args.location, args.port)
                        count_failed_connection_attempts = 1
                        print("Reconnected!")
                        break
                    except socket.error:
                        count_failed_connection_attempts += 1
                        print("The attempt failed. Making attempt %s." % (count_failed_connection_attempts))
                        time.sleep(args.connect_attempt_delay)
                else:
                    quit()

                continue

            # it is possible that more than one line has been received
            # so split it then loop through the parts and validate
            data = data_str.split("\n")
            for d in data:
                lines = d.split(",")
                #if the line has 22 items, it's valid
                if len(lines) == 22:
                    line = []
                    for r in lines:
                        lines = r.replace('\r', '')
                        line.append(lines)
                    # add the current time to the row
                    line.append(ds)
                    try:
                        line.append(icao_to_n(line[4]))
                    except:
                        pass
                    try:
                        if line[0] != 'MSG':
                            line[0] = 'MSG'
                        # add the row to the db
                        if not line[5] == '':
                            line[5] = int(line[5])
                        else:
                            line[5] = None
                        if not line[11] == '':
                            line[11] = int(line[11])
                        else:
                            line[11] = None
                        if not line[12] == '':
                            line[12] = float(line[12])
                        else:
                            line[12] = None
                        if not line[13] == '':
                            line[13] = float(line[13])
                        else:
                            line[13] = None
                        if not line[14] == '':
                            line[14] = float(line[14])
                        else:
                            line[14] = None
                        if not line[15] == '':
                            line[15] = float(line[15])
                        else:
                            line[15] = None
                        if not line[16] == '':
                            line[16] = float(line[16])
                        else:
                            line[16] = None
                        if not line[17] == '':
                            line[17] = int(line[17])
                        else:
                            line[17] = None
                        if not line[18] == '':
                            line[18] = int(line[18])
                        else:
                            line[18] = None
                        if not line[19] == '':
                            line[19] = int(line[19])
                        else:
                            line[19] = None
                        if not line[20] == '':
                            line[20] = int(line[20])
                        else:
                            line[20] = None
                        if not line[21] == '':
                            line[21] = int(line[21])
                        else:
                            line[21] = None
                        if line[14] != None:
                            adsb = (float(config.lat), float(config.lon))
                            plane = (line[14], line[15])
                            nm = haversine(adsb, plane, unit=Unit.NAUTICAL_MILES)
                            miles = haversine(adsb, plane, unit=Unit.MILES)
                        else:
                            nm = None
                            miles = None
                        line.append(nm)
                        line.append(miles)
                        cur.executemany("INSERT INTO squitters (message_type,transmission_type,session_id,aircraft_id,hex_ident,flight_id,generated_date,generated_time,logged_date,logged_time,callsign,altitude,ground_speed,track,lat,lon,vertical_rate,squawk,alert,emergency,spi,is_on_ground,parsed_time,n_num,distance_nm,distance_miles) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (line,))

                        # increment counts
                        count_total += 1
                        count_since_commit += 1

                        # commit the new rows to the database in batches
                        if count_since_commit % args.batch_size == 0:
                            conn.commit()
                            count_since_commit = 0

                    except psycopg2.OperationalError:
                        print(ts, "Could not write to database, will try to insert %s rows on next commit" % (count_since_commit + args.batch_size,))

                    except psycopg2.OperationalError:
                        print(ts, "Could not write to database, will try to insert %s rows on next commit" % (count_since_commit + args.batch_size,))
                    # since everything was valid we reset the stream message
                    data_str = ""
                else:
                    # the stream message is too short, prepend to the next stream message
                    data_str = d
                    continue

    except KeyboardInterrupt:
        print("\n%s Closing connection" % (ts,))
        s.close()

        conn.commit()
        conn.close()
        print(ts, "%s squitters added to your database" % (count_total))

    except psycopg2.ProgrammingError:
        print("Error with ", line)
        quit()

def connect_to_socket(loc,port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((loc, port))
    return s

def get_suffix(offset):
    """
    Compute the suffix for the tail number given an offset
    offset < suffix_size
    An offset of 0 returns in a valid emtpy suffix
    A non-zero offset return a string containing one or two character from 'charset'
    Reverse function of suffix_shift()
    0 -> ''
    1 -> 'A'
    2 -> 'AA'
    3 -> 'AB'
    4 -> 'AC'
    ...
    24 -> 'AZ'
    25 -> 'B'
    26 -> 'BA'
    27 -> 'BB'
    ...
    600 -> 'ZZ'
    """
    if offset==0:
        return ''
    char0 = charset[int((offset-1)/(len(charset)+1))]
    rem = (offset-1)%(len(charset)+1)
    if rem==0:
        return char0
    return char0+charset[rem-1]

def suffix_offset(s):
    """
    Compute the offset corresponding to the given alphabetical suffix
    Reverse function of get_suffix()
    ''   -> 0
    'A'  -> 1
    'AA' -> 2
    'AB' -> 3
    'AC' -> 4
    ...
    'AZ' -> 24
    'B'  -> 25
    'BA' -> 26
    'BB' -> 27
    ...
    'ZZ' -> 600
    """
    if len(s)==0:
        return 0
    valid = True
    if len(s)>2:
        valid = False
    else:
        for c in s:
            if c not in charset:
                valid = False
                break

    if not valid:
        print("parameter of suffix_shift() invalid")
        print(s)
        return None
    
    count = (len(charset)+1)*charset.index(s[0]) + 1
    if len(s)==2:
        count += charset.index(s[1]) + 1
    return count


def create_icao(prefix, i):
    """
    Creates an american icao number composed from the prefix ('a' for USA)
    and from the given number i
    The output is an hexadecimal of length 6 starting with the suffix
    Example: create_icao('a', 11) -> "a0000b"
    """
    suffix = hex(i)[2:]
    l = len(prefix)+len(suffix)
    if l>ICAO_SIZE:
        return None
    return prefix + '0'*(ICAO_SIZE-l) + suffix

def n_to_icao(nnumber):
    """
    Convert a Tail Number (N-Number) to the corresponding ICAO address
    Only works with US registrations (ICAOS starting with 'a' and tail number starting with 'N')
    Return None for invalid parameter
    Return the ICAO address associated with the given N-Number in string format on success
    """

    # check parameter validity
    valid = True
    if (not 0<len(nnumber)<=NNUMBER_MAX_SIZE) or nnumber[0] != 'N':
        valid = False
    else:
        for c in nnumber:
            if c not in allchars:
                valid = False
                break
    if not valid:
        return None
    
    prefix = 'a'
    count = 0

    if len(nnumber) > 1:
        nnumber = nnumber[1:]
        count += 1
        for i in range(len(nnumber)):
            if i == NNUMBER_MAX_SIZE-2: # NNUMBER_MAX_SIZE-2 = 4
                # last possible char (in allchars)
                count += allchars.index(nnumber[i])+1
            elif nnumber[i] in charset:
                # first alphabetical char
                count += suffix_offset(nnumber[i:])
                break # nothing comes after alphabetical chars
            else:
                # number
                if i == 0:
                    count += (int(nnumber[i])-1)*bucket1_size
                elif i == 1:
                    count += int(nnumber[i])*bucket2_size + suffix_size
                elif i == 2:
                    count += int(nnumber[i])*bucket3_size + suffix_size
                elif i == 3:
                    count += int(nnumber[i])*bucket4_size + suffix_size
    return create_icao(prefix, count)

def icao_to_n(icao):
    """
    Convert an ICAO address to its associated tail number (N-Number)
    Only works with US registrations (ICAOS starting with 'a' and tail number starting with 'N')
    Return None for invalid parameter
    Return the tail number associated with the given ICAO in string format on success
    """

    # check parameter validity
    icao = icao.upper()
    valid = True
    if len(icao) != ICAO_SIZE or icao[0] != 'A':
        valid = False
    else:
        for c in icao:
            if c not in hexset:
                valid = False
                break
    
    # return None for invalid parameter
    if not valid:
        return None

    output = 'N' # digit 0 = N

    i = int(icao[1:], base=16)-1 # parse icao to int
    if i < 0:
        return output

    dig1 = int(i/bucket1_size) + 1 # digit 1
    rem1 = i%bucket1_size
    output += str(dig1)

    if rem1 < suffix_size:
        return output + get_suffix(rem1)

    rem1 -= suffix_size # shift for digit 2
    dig2 = int(rem1/bucket2_size)
    rem2 = rem1%bucket2_size
    output += str(dig2)

    if rem2 < suffix_size:
        return output + get_suffix(rem2)

    rem2 -= suffix_size # shift for digit 3
    dig3 = int(rem2/bucket3_size)
    rem3 = rem2%bucket3_size
    output += str(dig3)

    if rem3 < suffix_size:
        return output + get_suffix(rem3)

    rem3 -= suffix_size # shift for digit 4
    dig4 = int(rem3/bucket4_size)
    rem4 = rem3%bucket4_size
    output += str(dig4)

    if rem4 == 0:
        return output

    # find last character
    return output + allchars[rem4-1]


if __name__ == '__main__':
    main()
