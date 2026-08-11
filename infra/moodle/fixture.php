<?php

declare(strict_types=1);

if (!defined('AUTOTASK_FIXTURE_LIBRARY')) {
    define('CLI_SCRIPT', true);
    require '/var/www/html/config.php';
    require_once $CFG->dirroot . '/user/lib.php';
    require_once $CFG->dirroot . '/course/lib.php';
    require_once $CFG->dirroot . '/course/modlib.php';
}

const AUTOTASK_FIXTURE_CONFIG = 'moddle_autotask_rich_fixture_version';
const AUTOTASK_FIXTURE_ANCHOR_CONFIG = 'moddle_autotask_rich_fixture_anchor';
const AUTOTASK_FIXTURE_CATALOG_DIGEST_CONFIG = 'moddle_autotask_rich_fixture_catalog_v3_sha256';
const AUTOTASK_FIXTURE_V3_COURSE = 'ASIX-CAMPAIGN-01';

function fixture_courses(): array {
    return [
        'ASIX1-0369-ISO' => '1-0369 - Implantació de Sistemes Operatius',
        'ASIX1-0371-FM' => '1-0371 - Fonaments de Maquinari',
        'ASIX1-0372-GBD' => '1-0372 - Gestió de Bases de Dades',
        'ASIX1-0373-LMSGI' => '1-0373 - Llenguatges de Marques i Sistemes de Gestió',
        'ASIX1-0376-IAW' => "1-0376 - Implantació d'Aplicacions Web",
        'ASIX1-0377-ASGBD' => '1-0377 - Administració de Sistemes Gestors de Bases de Dades',
        'ASIX2-0370-PAX' => '2-0370 - Planificació i Administració de Xarxes',
        'ASIX2-0374-ASO' => '2-0374 - Administració de Sistemes Operatius',
        'ASIX2-0375-SXI' => "2-0375 - Serveis de Xarxa i Internet",
        'ASIX2-0378-SAD' => '2-0378 - Seguretat i Alta Disponibilitat',
        'ASIX2-0379-PROJ' => "2-0379 - Projecte d'Administració de Sistemes",
    ];
}

function fixture_assignments(): array {
    return [
        [
            'course' => 'ASIX1-0369-ISO',
            'idnumber' => 'autotask-rich-iso-ova',
            'name' => "Pràctica ISO 1 - Desplegament d'una OVA",
            'intro' => '<p>Importa la màquina virtual simulada, crea una xarxa interna, verifica la connectivitat i documenta cada pas amb captures.</p>',
            'dueoffset' => 172800,
            'allowoffset' => -86400,
            'files' => [
                'practica-iso-ova.pdf' => "%PDF-1.4\n% AutoTask deterministic PDF fixture\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n",
                'asix-router-lab.ova' => "AUTOTASK-SIMULATED-OVA\nThis tiny deterministic fixture is metadata-only and is not bootable.\n",
            ],
        ],
        [
            'course' => 'ASIX1-0369-ISO',
            'idnumber' => 'autotask-rich-iso-permissions',
            'name' => 'Pràctica ISO 2 - Usuaris, grups i permisos Linux',
            'intro' => '<p>Resol una incidència de permisos POSIX i ACL. Aquesta tasca està vençuda per provar alertes de termini.</p>',
            'dueoffset' => -86400,
            'allowoffset' => -604800,
            'files' => [],
        ],
        [
            'course' => 'ASIX1-0372-GBD',
            'idnumber' => 'autotask-rich-gbd-backup',
            'name' => 'Pràctica GBD - Còpia i restauració de PostgreSQL',
            'intro' => '<p>Crea una base de dades, executa la còpia lògica, restaura-la i adjunta evidències de les comprovacions.</p>',
            'dueoffset' => 432000,
            'allowoffset' => -86400,
            'files' => ['inventari.sql' => "CREATE TABLE inventari (id integer PRIMARY KEY, nom text NOT NULL);\nINSERT INTO inventari VALUES (1, 'router');\n"],
        ],
        [
            'course' => 'ASIX1-0373-LMSGI',
            'idnumber' => 'autotask-rich-lmsgi-xml',
            'name' => 'Pràctica LMSGI - Validació XML amb XSD',
            'intro' => '<p>Valida el document XML, identifica els errors i genera una versió corregida.</p>',
            'dueoffset' => 604800,
            'allowoffset' => 0,
            'files' => [
                'servidors.xml' => "<?xml version=\"1.0\"?><servidors><servidor id=\"1\">web01</servidor></servidors>\n",
                'servidors.xsd' => "<?xml version=\"1.0\"?><xs:schema xmlns:xs=\"http://www.w3.org/2001/XMLSchema\"><xs:element name=\"servidors\" type=\"xs:string\"/></xs:schema>\n",
            ],
        ],
        [
            'course' => 'ASIX1-0376-IAW',
            'idnumber' => 'autotask-rich-iaw-moodle',
            'name' => 'Pràctica IAW - Desplegament web amb contenidors',
            'intro' => '<p>Desplega una aplicació web amb Docker Compose, comprova la persistència i prepara un informe de recuperació.</p>',
            'dueoffset' => 864000,
            'allowoffset' => 259200,
            'files' => ['compose.yml' => "services:\n  web:\n    image: nginx:1.27-alpine\n    ports:\n      - '8080:80'\n"],
        ],
        [
            'course' => 'ASIX1-0377-ASGBD',
            'idnumber' => 'autotask-rich-asgbd-replication',
            'name' => 'Pràctica ASGBD - Pla de replicació',
            'intro' => '<p>Dissenya una topologia primari-rèplica, justifica RPO i RTO, i documenta una prova de commutació.</p>',
            'dueoffset' => 1209600,
            'allowoffset' => 0,
            'files' => ['requisits-replicacio.txt' => "RPO objectiu: 5 minuts\nRTO objectiu: 20 minuts\nDades de prova exclusivament fictícies.\n"],
        ],
        [
            'course' => 'ASIX2-0370-PAX',
            'idnumber' => 'autotask-rich-pax-vlans',
            'name' => 'Pràctica PAX - VLAN, routing i diagnòstic',
            'intro' => '<p>Configura tres VLAN, routing inter-VLAN i una política mínima de filtratge. Sense data límit.</p>',
            'dueoffset' => 0,
            'allowoffset' => 0,
            'files' => ['topologia.txt' => "VLAN 10 ALUMNES 10.10.10.0/24\nVLAN 20 SERVEIS 10.10.20.0/24\nVLAN 30 GESTIO 10.10.30.0/24\n"],
        ],
        [
            'course' => 'ASIX2-0374-ASO',
            'idnumber' => 'autotask-rich-aso-ansible',
            'name' => 'Pràctica ASO - Automatització amb Ansible',
            'intro' => '<p>Completa el playbook perquè sigui idempotent i verifica dues execucions consecutives.</p>',
            'dueoffset' => 691200,
            'allowoffset' => -172800,
            'files' => ['site.yml' => "---\n- hosts: all\n  gather_facts: false\n  tasks:\n    - debug:\n        msg: autotask fixture\n"],
        ],
        [
            'course' => 'ASIX2-0375-SXI',
            'idnumber' => 'autotask-rich-sxi-dns',
            'name' => 'Pràctica SXI - DNS autoritatiu i resolució',
            'intro' => '<p>Configura una zona directa i inversa, valida-la i captura les consultes de diagnòstic.</p>',
            'dueoffset' => 950400,
            'allowoffset' => 0,
            'files' => ['db.example.test' => "\$ORIGIN example.test.\n@ 3600 IN SOA ns.example.test. admin.example.test. (1 3600 900 604800 300)\n@ IN NS ns.example.test.\nns IN A 192.0.2.53\n"],
        ],
        [
            'course' => 'ASIX2-0378-SAD',
            'idnumber' => 'autotask-rich-sad-hardening',
            'name' => 'Pràctica SAD - Hardening de Windows Server',
            'intro' => '<p>Aplica una línia base de seguretat fictícia, comprova serveis i genera evidències abans/després.</p>',
            'dueoffset' => 1296000,
            'allowoffset' => 0,
            'files' => ['baseline.ps1' => "Set-StrictMode -Version Latest\nWrite-Output 'AutoTask deterministic hardening fixture'\n"],
        ],
        [
            'course' => 'ASIX2-0379-PROJ',
            'idnumber' => 'autotask-rich-projecte',
            'name' => 'Projecte ASIX - Lliurament de la proposta tècnica',
            'intro' => '<p>Entrega arquitectura, riscos, pressupost, pla de proves i estratègia de recuperació. Requereix revisió humana.</p>',
            'dueoffset' => 1814400,
            'allowoffset' => 0,
            'files' => ['plantilla-projecte.md' => "# Proposta tècnica\n\n## Arquitectura\n## Riscos\n## Pressupost\n## Proves\n## Recuperació\n"],
        ],
    ];
}

function fixture_footprint_exists(): bool {
    global $DB;
    $coursekeys = array_keys(fixture_courses());
    if ($DB->record_exists_select('course', 'shortname IN (' . implode(',', array_fill(0, count($coursekeys), '?')) . ')', $coursekeys)) {
        return true;
    }
    return $DB->record_exists_select('course_categories', 'idnumber IN (?, ?, ?)', ['AUTOTASK-CF', 'AUTOTASK-INFO', 'AUTOTASK-ASIX'])
        || $DB->record_exists_select('course_modules', $DB->sql_like('idnumber', '?'), ['autotask-rich-%'])
        || fixture_v3_footprint_exists();
}

function fixture_v3_footprint_exists(): bool {
    global $DB;
    return get_config('core', AUTOTASK_FIXTURE_CATALOG_DIGEST_CONFIG) !== false
        || $DB->record_exists('course', ['shortname' => AUTOTASK_FIXTURE_V3_COURSE])
        || $DB->record_exists_select('user', 'username IN (' . implode(',', array_fill(0, 15, '?')) . ')', fixture_v3_usernames())
        || $DB->record_exists_select('course_modules', 'idnumber IN (?, ?, ?, ?)', [
            'central-report-success', 'windows-ssm-success', 'windows-command-failure', 'ova-import-negative',
        ]);
}

function fixture_timestamp(int $anchor, int $offset): int {
    return $offset === 0 ? 0 : $anchor + $offset;
}

function infer_fixture_anchor(bool $advanced): ?int {
    global $DB;
    $due = $DB->get_field_sql(
        "SELECT a.duedate FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module WHERE cm.idnumber = ? AND m.name = 'assign'",
        ['autotask-rich-iso-ova'],
    );
    if ($due === false) {
        return null;
    }
    $anchor = (int)$due - 172800 - ($advanced ? 86400 : 0);
    return $anchor > 0 ? $anchor : null;
}

function fixture_anchor(bool $advanced): ?int {
    $stored = get_config('core', AUTOTASK_FIXTURE_ANCHOR_CONFIG);
    if ($stored === false || $stored === '') {
        return infer_fixture_anchor($advanced);
    }
    if (!preg_match('/^[1-9][0-9]*$/', (string)$stored)) {
        return null;
    }
    return (int)$stored;
}

function verify_fixture(bool $advanced, int $anchor): bool {
    global $DB;
    try {
        $student = $DB->get_record('user', ['username' => 'student1'], '*', MUST_EXIST);
        $root = $DB->get_record('course_categories', ['idnumber' => 'AUTOTASK-CF'], '*', MUST_EXIST);
        $info = $DB->get_record('course_categories', ['idnumber' => 'AUTOTASK-INFO'], '*', MUST_EXIST);
        $asix = $DB->get_record('course_categories', ['idnumber' => 'AUTOTASK-ASIX'], '*', MUST_EXIST);
        if ($root->name !== 'Cicles formatius' || (int)$root->parent !== 0
                || $info->name !== 'Informàtica i comunicacions' || (int)$info->parent !== (int)$root->id
                || $asix->name !== 'ASIX - Administració de Sistemes Informàtics en Xarxa'
                || (int)$asix->parent !== (int)$info->id) {
            return false;
        }
        $reservedmodules = $DB->count_records_select(
            'course_modules',
            $DB->sql_like('idnumber', '?'),
            ['autotask-rich-%'],
        );
        if ($reservedmodules !== count(fixture_assignments())) {
            return false;
        }
        $courses = fixture_courses();
        foreach ($courses as $shortname => $fullname) {
            $course = $DB->get_record('course', ['shortname' => $shortname], '*', MUST_EXIST);
            if ($course->fullname !== $fullname || (int)$course->category !== (int)$asix->id
                    || $course->summary !== 'Curs fictici i determinista per a proves locals de Moodle Autotask.') {
                return false;
            }
            $enrolled = is_enrolled(context_course::instance($course->id), $student, '', true);
            if (!$enrolled) {
                return false;
            }
        }
        foreach (fixture_assignments() as $spec) {
            $row = $DB->get_record_sql(
                "SELECT a.*, cm.id AS cmid FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module JOIN {course} c ON c.id = a.course WHERE c.shortname = ? AND cm.idnumber = ? AND m.name = 'assign'",
                [$spec['course'], $spec['idnumber']],
                MUST_EXIST,
            );
            $expectedintro = $spec['intro'];
            if ($spec['idnumber'] === 'autotask-rich-iso-ova' && $advanced) {
                $expectedintro .= '<p><strong>Actualització:</strong> afegeix una segona interfície i documenta la ruta de retorn.</p>';
            }
            $expecteddue = fixture_timestamp($anchor, $spec['dueoffset']);
            if ($spec['idnumber'] === 'autotask-rich-iso-ova' && $advanced) {
                $expecteddue += 86400;
            }
            $expectedallow = fixture_timestamp($anchor, $spec['allowoffset']);
            if ($row->name !== $spec['name'] || $row->intro !== $expectedintro
                    || (int)$row->duedate !== $expecteddue
                    || (int)$row->allowsubmissionsfromdate !== $expectedallow) {
                return false;
            }
            $context = context_module::instance($row->cmid);
            $expectedfiles = $spec['files'];
            if ($spec['idnumber'] === 'autotask-rich-iso-ova' && $advanced) {
                $expectedfiles['revision-2.txt'] = "Fixture revision 2: add a second interface and verify the return route.\n";
            }
            $actual = get_file_storage()->get_area_files($context->id, 'mod_assign', 'introattachment', 0, 'filename', false);
            if (count($actual) !== count($expectedfiles)) {
                return false;
            }
            foreach ($actual as $file) {
                $filename = $file->get_filename();
                if (!array_key_exists($filename, $expectedfiles) || $file->get_content() !== $expectedfiles[$filename]) {
                    return false;
                }
            }
        }
        return true;
    } catch (Throwable) {
        return false;
    }
}

function fixture_state(): string {
    $version = get_config('core', AUTOTASK_FIXTURE_CONFIG);
    if ($version === false || $version === '') {
        return fixture_footprint_exists() ? 'partial' : 'absent';
    }
    if ($version === '3') {
        $catalog = fixture_v3_catalog();
        if ($catalog !== null && verify_fixture_v3($catalog['data'], $catalog['digest'], true)) {
            return 'complete-v3';
        }
        return $catalog !== null && verify_fixture_v3($catalog['data'], $catalog['digest'], true, $ignored, false)
            ? 'complete-v3-submission-config-legacy' : 'partial';
    }
    if ($version !== '1' && $version !== '2') {
        return 'partial';
    }
    if (fixture_v3_footprint_exists()) {
        return 'partial';
    }
    $advanced = $version === '2';
    $anchor = ($version === '1' || $advanced) ? fixture_anchor($advanced) : null;
    if ($anchor !== null && verify_fixture($advanced, $anchor)) {
        return 'complete-v' . $version;
    }
    return 'partial';
}

function create_assignment(object $course, array $spec, int $now): void {
    global $DB;
    $module = $DB->get_record('modules', ['name' => 'assign'], '*', MUST_EXIST);
    $moduleinfo = (object) [
        'modulename' => 'assign', 'module' => $module->id, 'course' => $course->id,
        'section' => 0, 'visible' => 1, 'showdescription' => 0, 'name' => $spec['name'],
        'cmidnumber' => $spec['idnumber'], 'intro' => $spec['intro'], 'introformat' => FORMAT_HTML,
        'alwaysshowdescription' => 1, 'submissionattachments' => !empty($spec['submissionfile']) ? 1 : 0, 'submissiondrafts' => 0,
        'assignsubmission_file_enabled' => !empty($spec['submissionfile']) ? 1 : 0,
        'assignsubmission_file_maxfiles' => !empty($spec['submissionfile']) ? 1 : 0,
        'assignsubmission_file_maxsizebytes' => !empty($spec['submissionfile']) ? 2097152 : 0,
        'assignsubmission_file_filetypes' => !empty($spec['submissionfile']) ? '.md' : '',
        'assignsubmission_onlinetext_enabled' => 0,
        'requiresubmissionstatement' => 0, 'sendnotifications' => 0, 'sendlatenotifications' => 0,
        'sendstudentnotifications' => 0,
        'duedate' => fixture_timestamp($now, $spec['dueoffset']),
        'allowsubmissionsfromdate' => fixture_timestamp($now, $spec['allowoffset']),
        'grade' => 100, 'completionsubmit' => 0, 'cutoffdate' => 0, 'gradingduedate' => 0,
        'teamsubmission' => 0, 'requireallteammemberssubmit' => 0, 'teamsubmissiongroupingid' => 0,
        'blindmarking' => 0, 'hidegrader' => 0, 'markingworkflow' => 0, 'markingallocation' => 0,
        'preventsubmissionnotingroup' => 0, 'attemptreopenmethod' => 'untilpass', 'maxattempts' => 1,
        'markinganonymous' => 0, 'timelimit' => 0, 'gradepenalty' => 0, 'completion' => 0,
        'completionexpected' => 0,
    ];
    add_moduleinfo($moduleinfo, $course);
    $cmid = $DB->get_field('course_modules', 'id', ['course' => $course->id, 'idnumber' => $spec['idnumber']], MUST_EXIST);
    $context = context_module::instance($cmid);
    foreach ($spec['files'] as $filename => $content) {
        get_file_storage()->create_file_from_string((object) [
            'contextid' => $context->id, 'component' => 'mod_assign', 'filearea' => 'introattachment',
            'itemid' => 0, 'filepath' => '/', 'filename' => $filename, 'userid' => get_admin()->id,
        ], $content);
    }
}

function seed_fixture(): void {
    global $DB;
    if (fixture_state() !== 'absent') {
        throw new RuntimeException('rich fixture is not absent');
    }
    $student = $DB->get_record('user', ['username' => 'student1'], '*', MUST_EXIST);
    $root = core_course_category::create(['name' => 'Cicles formatius', 'idnumber' => 'AUTOTASK-CF', 'parent' => 0]);
    $info = core_course_category::create(['name' => "Informàtica i comunicacions", 'idnumber' => 'AUTOTASK-INFO', 'parent' => $root->id]);
    $asix = core_course_category::create(['name' => 'ASIX - Administració de Sistemes Informàtics en Xarxa', 'idnumber' => 'AUTOTASK-ASIX', 'parent' => $info->id]);
    $role = $DB->get_record('role', ['shortname' => 'student'], '*', MUST_EXIST);
    $manual = enrol_get_plugin('manual');
    if (!$manual) {
        throw new RuntimeException('manual enrolment plugin unavailable');
    }
    $created = [];
    foreach (fixture_courses() as $shortname => $fullname) {
        $course = create_course((object) ['fullname' => $fullname, 'shortname' => $shortname, 'category' => $asix->id, 'summary' => 'Curs fictici i determinista per a proves locals de Moodle Autotask.']);
        $instance = $DB->get_record('enrol', ['courseid' => $course->id, 'enrol' => 'manual'], '*', MUST_EXIST);
        $manual->enrol_user($instance, $student->id, $role->id);
        $created[$shortname] = $course;
    }
    $now = time();
    foreach (fixture_assignments() as $spec) {
        create_assignment($created[$spec['course']], $spec, $now);
    }
    set_config(AUTOTASK_FIXTURE_ANCHOR_CONFIG, (string)$now);
    set_config(AUTOTASK_FIXTURE_CONFIG, '1');
    if (!verify_fixture(false, $now)) {
        throw new RuntimeException('rich fixture verification failed after seed');
    }
}

function advance_fixture(): void {
    global $DB;
    if (fixture_state() !== 'complete-v1') {
        throw new RuntimeException('rich fixture must be at revision 1');
    }
    $anchor = fixture_anchor(false);
    if ($anchor === null) {
        throw new RuntimeException('rich fixture anchor is invalid');
    }
    if (get_config('core', AUTOTASK_FIXTURE_ANCHOR_CONFIG) === false) {
        set_config(AUTOTASK_FIXTURE_ANCHOR_CONFIG, (string)$anchor);
    }
    $idnumber = 'autotask-rich-iso-ova';
    $row = $DB->get_record_sql(
        "SELECT a.*, cm.id AS cmid FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module WHERE cm.idnumber = ? AND m.name = 'assign'",
        [$idnumber],
        MUST_EXIST,
    );
    $row->intro .= '<p><strong>Actualització:</strong> afegeix una segona interfície i documenta la ruta de retorn.</p>';
    $row->duedate += 86400;
    $row->timemodified = time();
    $DB->update_record('assign', $row);
    get_file_storage()->create_file_from_string((object) [
        'contextid' => context_module::instance($row->cmid)->id,
        'component' => 'mod_assign', 'filearea' => 'introattachment', 'itemid' => 0,
        'filepath' => '/', 'filename' => 'revision-2.txt', 'userid' => get_admin()->id,
    ], "Fixture revision 2: add a second interface and verify the return route.\n");
    set_config(AUTOTASK_FIXTURE_CONFIG, '2');
    if (!verify_fixture(true, $anchor)) {
        throw new RuntimeException('rich fixture verification failed after advance');
    }
}

/** @return list<string> */
function fixture_v3_usernames(): array {
    return [
        'teacher.ada', 'teacher.grace', 'teacher.luis', 'teacher.nora',
        'student2', 'student3', 'student4', 'student5', 'student6', 'student7',
        'student8', 'student9', 'student10', 'student11', 'student12',
    ];
}

function fixture_v3_assert_exact_keys(array $value, array $keys, string $where): void {
    $actual = array_keys($value);
    sort($actual);
    sort($keys);
    if ($actual !== $keys) {
        throw new InvalidArgumentException('invalid v3 catalog keys at ' . $where);
    }
}

function fixture_v3_string(mixed $value, string $where): string {
    if (!is_string($value) || $value === '' || trim($value) !== $value || str_contains($value, "\0")) {
        throw new InvalidArgumentException('invalid v3 catalog string at ' . $where);
    }
    return $value;
}

function fixture_v3_canonicalize(mixed $value): mixed {
    if (!is_array($value)) {
        return $value;
    }
    if (array_is_list($value)) {
        return array_map('fixture_v3_canonicalize', $value);
    }
    ksort($value, SORT_STRING);
    foreach ($value as $key => $item) {
        $value[$key] = fixture_v3_canonicalize($item);
    }
    return $value;
}

function fixture_v3_validate_catalog(mixed $catalog): array {
    if (!is_array($catalog)) {
        throw new InvalidArgumentException('v3 catalog must be an object');
    }
    fixture_v3_assert_exact_keys($catalog, ['assignments', 'course', 'schemaVersion', 'students', 'teachers'], 'root');
    if (($catalog['schemaVersion'] ?? null) !== 3 || !is_array($catalog['course'])
            || !is_array($catalog['teachers']) || !is_array($catalog['students']) || !is_array($catalog['assignments'])) {
        throw new InvalidArgumentException('invalid v3 catalog schema');
    }
    fixture_v3_assert_exact_keys($catalog['course'], ['fullname', 'shortname', 'summary'], 'course');
    if (fixture_v3_string($catalog['course']['shortname'], 'course.shortname') !== AUTOTASK_FIXTURE_V3_COURSE) {
        throw new InvalidArgumentException('invalid v3 campaign course identity');
    }
    fixture_v3_string($catalog['course']['fullname'], 'course.fullname');
    fixture_v3_string($catalog['course']['summary'], 'course.summary');
    if (count($catalog['teachers']) !== 4 || count($catalog['students']) !== 11 || count($catalog['assignments']) !== 4) {
        throw new InvalidArgumentException('invalid v3 catalog cardinality');
    }
    $usernames = [];
    foreach (['teachers', 'students'] as $kind) {
        foreach ($catalog[$kind] as $index => $identity) {
            if (!is_array($identity)) {
                throw new InvalidArgumentException('invalid v3 ' . $kind . ' identity');
            }
            fixture_v3_assert_exact_keys($identity, ['email', 'firstname', 'lastname', 'username'], $kind . '[' . $index . ']');
            foreach (['username', 'firstname', 'lastname', 'email'] as $field) {
                fixture_v3_string($identity[$field], $kind . '[' . $index . '].' . $field);
            }
            $username = $identity['username'];
            if (!preg_match('/^[a-z][a-z0-9.]{1,30}$/', $username)
                    || !preg_match('/^[a-z0-9.]+@example\\.test$/', $identity['email'])
                    || isset($usernames[$username])) {
                throw new InvalidArgumentException('invalid or duplicate v3 identity');
            }
            $usernames[$username] = true;
        }
    }
    if (array_keys($usernames) !== fixture_v3_usernames()) {
        throw new InvalidArgumentException('unexpected v3 identity set');
    }
    $assignmentids = [];
    foreach ($catalog['assignments'] as $index => $assignment) {
        if (!is_array($assignment)) {
            throw new InvalidArgumentException('invalid v3 assignment');
        }
        fixture_v3_assert_exact_keys($assignment, ['allowoffset', 'dueoffset', 'files', 'idnumber', 'intro', 'scenario', 'title'], 'assignments[' . $index . ']');
        foreach (['idnumber', 'title', 'intro', 'scenario'] as $field) {
            fixture_v3_string($assignment[$field], 'assignments[' . $index . '].' . $field);
        }
        if (!in_array($assignment['scenario'], ['CENTRAL', 'HYBRID', 'IN_GUEST'], true)
                || !preg_match('/^[a-z][a-z0-9-]{2,63}$/', $assignment['idnumber'])
                || isset($assignmentids[$assignment['idnumber']])
                || !is_int($assignment['dueoffset']) || !is_int($assignment['allowoffset'])
                || $assignment['dueoffset'] <= 0 || $assignment['allowoffset'] < 0 || $assignment['allowoffset'] > $assignment['dueoffset']
                || !is_array($assignment['files']) || count($assignment['files']) !== 1) {
            throw new InvalidArgumentException('invalid v3 assignment metadata');
        }
        foreach ($assignment['files'] as $filename => $contents) {
            if (!is_string($filename) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/', $filename)
                    || !is_string($contents) || $contents === '' || strlen($contents) > 16384) {
                throw new InvalidArgumentException('invalid v3 assignment file');
            }
        }
        $assignmentids[$assignment['idnumber']] = true;
    }
    if (array_keys($assignmentids) !== [
        'central-report-success', 'windows-ssm-success', 'windows-command-failure', 'ova-import-negative',
    ]) {
        throw new InvalidArgumentException('unexpected v3 assignment identities');
    }
    return $catalog;
}

/** @return array{data: array, digest: string}|null */
function fixture_v3_catalog(): ?array {
    return $GLOBALS['moddle_autotask_fixture_v3_catalog'] ?? null;
}

function fixture_v3_json_whitespace(string $raw, int &$offset): void {
    while ($offset < strlen($raw) && str_contains(" \t\r\n", $raw[$offset])) {
        $offset++;
    }
}

function fixture_v3_json_string(string $raw, int &$offset): string {
    if (($raw[$offset] ?? '') !== '"') {
        throw new InvalidArgumentException('v3 catalog is not valid JSON');
    }
    $start = $offset++;
    $length = strlen($raw);
    while ($offset < $length) {
        $character = $raw[$offset++];
        if ($character === '"') {
            try {
                $decoded = json_decode(substr($raw, $start, $offset - $start), true, 512, JSON_THROW_ON_ERROR);
            } catch (JsonException $error) {
                throw new InvalidArgumentException('v3 catalog is not valid JSON', 0, $error);
            }
            if (!is_string($decoded)) {
                throw new InvalidArgumentException('v3 catalog is not valid JSON');
            }
            return $decoded;
        }
        if (ord($character) < 0x20) {
            throw new InvalidArgumentException('v3 catalog is not valid JSON');
        }
        if ($character !== '\\') {
            continue;
        }
        if ($offset >= $length) {
            throw new InvalidArgumentException('v3 catalog is not valid JSON');
        }
        $escape = $raw[$offset++];
        if (str_contains('"\\/bfnrt', $escape)) {
            continue;
        }
        if ($escape !== 'u' || $offset + 4 > $length || !ctype_xdigit(substr($raw, $offset, 4))) {
            throw new InvalidArgumentException('v3 catalog is not valid JSON');
        }
        $offset += 4;
    }
    throw new InvalidArgumentException('v3 catalog is not valid JSON');
}

function fixture_v3_json_value(string $raw, int &$offset): void {
    fixture_v3_json_whitespace($raw, $offset);
    $character = $raw[$offset] ?? '';
    if ($character === '{') {
        $offset++;
        fixture_v3_json_whitespace($raw, $offset);
        $keys = [];
        if (($raw[$offset] ?? '') === '}') {
            $offset++;
            return;
        }
        while (true) {
            fixture_v3_json_whitespace($raw, $offset);
            $key = fixture_v3_json_string($raw, $offset);
            $identity = "\0" . $key;
            if (isset($keys[$identity])) {
                throw new InvalidArgumentException('v3 catalog contains duplicate object keys');
            }
            $keys[$identity] = true;
            fixture_v3_json_whitespace($raw, $offset);
            if (($raw[$offset++] ?? '') !== ':') {
                throw new InvalidArgumentException('v3 catalog is not valid JSON');
            }
            fixture_v3_json_value($raw, $offset);
            fixture_v3_json_whitespace($raw, $offset);
            $delimiter = $raw[$offset++] ?? '';
            if ($delimiter === '}') {
                return;
            }
            if ($delimiter !== ',') {
                throw new InvalidArgumentException('v3 catalog is not valid JSON');
            }
        }
    }
    if ($character === '[') {
        $offset++;
        fixture_v3_json_whitespace($raw, $offset);
        if (($raw[$offset] ?? '') === ']') {
            $offset++;
            return;
        }
        while (true) {
            fixture_v3_json_value($raw, $offset);
            fixture_v3_json_whitespace($raw, $offset);
            $delimiter = $raw[$offset++] ?? '';
            if ($delimiter === ']') {
                return;
            }
            if ($delimiter !== ',') {
                throw new InvalidArgumentException('v3 catalog is not valid JSON');
            }
        }
    }
    if ($character === '"') {
        fixture_v3_json_string($raw, $offset);
        return;
    }
    foreach (['true', 'false', 'null'] as $literal) {
        if (substr($raw, $offset, strlen($literal)) === $literal) {
            $offset += strlen($literal);
            return;
        }
    }
    if (preg_match('/\G-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/', $raw, $match, 0, $offset)) {
        $offset += strlen($match[0]);
        return;
    }
    throw new InvalidArgumentException('v3 catalog is not valid JSON');
}

function fixture_v3_reject_duplicate_json_keys(string $raw): void {
    $offset = 0;
    fixture_v3_json_value($raw, $offset);
    fixture_v3_json_whitespace($raw, $offset);
    if ($offset !== strlen($raw)) {
        throw new InvalidArgumentException('v3 catalog is not valid JSON');
    }
}

function fixture_v3_load_catalog(string $path): void {
    if ($path === '' || !is_file($path) || is_link($path) || !is_readable($path)) {
        throw new InvalidArgumentException('v3 catalog path is invalid');
    }
    $raw = file_get_contents($path);
    if ($raw === false || strlen($raw) > 262144) {
        throw new InvalidArgumentException('v3 catalog cannot be read');
    }
    try {
        fixture_v3_reject_duplicate_json_keys($raw);
        $catalog = fixture_v3_validate_catalog(json_decode($raw, true, 512, JSON_THROW_ON_ERROR));
    } catch (JsonException $error) {
        throw new InvalidArgumentException('v3 catalog is not valid JSON', 0, $error);
    }
    $canonical = json_encode(fixture_v3_canonicalize($catalog), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    $GLOBALS['moddle_autotask_fixture_v3_catalog'] = ['data' => $catalog, 'digest' => hash('sha256', $canonical)];
}

function fixture_v3_verify_files(int $contextid, array $expected): bool {
    $actual = get_file_storage()->get_area_files($contextid, 'mod_assign', 'introattachment', 0, 'filename', false);
    if (count($actual) !== count($expected)) {
        return false;
    }
    foreach ($actual as $file) {
        if (!isset($expected[$file->get_filename()]) || $file->get_content() !== $expected[$file->get_filename()]) {
            return false;
        }
    }
    return true;
}

function fixture_v3_has_exact_assignments(int $courseid, array $expected): bool {
    global $DB;
    $actual = $DB->get_fieldset_sql(
        "SELECT cm.idnumber FROM {course_modules} cm JOIN {modules} m ON m.id = cm.module WHERE cm.course = ? AND m.name = ?",
        [$courseid, 'assign']
    );
    sort($actual, SORT_STRING);
    sort($expected, SORT_STRING);
    return $actual === $expected;
}

function fixture_v3_submission_file_enabled(int $assignmentid): bool {
    global $DB;
    $rows = $DB->get_records('assign_plugin_config', [
        'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'file',
    ], '', 'name,value');
    $expected = [
        'enabled' => '1', 'maxfilesubmissions' => '1',
        'maxsubmissionsizebytes' => '2097152', 'filetypeslist' => '.md',
    ];
    if (count($rows) < count($expected)) {
        return false;
    }
    foreach ($expected as $name => $value) {
        if (!isset($rows[$name]) || (string)$rows[$name]->value !== $value) {
            return false;
        }
    }
    $online = $DB->get_field('assign_plugin_config', 'value', [
        'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'onlinetext', 'name' => 'enabled',
    ]);
    return (string)$online === '0';
}

function fixture_v3_verification_failure(?string &$reason, string $code): bool {
    $reason = $code;
    return false;
}

function verify_fixture_v3(array $catalog, string $digest, bool $requirestored, ?string &$reason = null, bool $checksubmission = true): bool {
    global $DB;
    $reason = null;
    $phase = 'legacy-v2';
    try {
        $anchor = fixture_anchor(true);
        if ($anchor === null || !verify_fixture(true, $anchor)) {
            return fixture_v3_verification_failure($reason, 'legacy-v2');
        }
        $stored = get_config('core', AUTOTASK_FIXTURE_CATALOG_DIGEST_CONFIG);
        if (($requirestored && (!is_string($stored) || !hash_equals($digest, $stored))) || (!$requirestored && $stored !== false && !hash_equals($digest, (string)$stored))) {
            return fixture_v3_verification_failure($reason, 'catalog-digest');
        }
        $phase = 'campaign-course';
        $campaign = $DB->get_record('course', ['shortname' => AUTOTASK_FIXTURE_V3_COURSE]);
        $asix = $DB->get_record('course_categories', ['idnumber' => 'AUTOTASK-ASIX']);
        if (!$campaign || !$asix) {
            return fixture_v3_verification_failure($reason, 'campaign-course');
        }
        if ($campaign->fullname !== $catalog['course']['fullname'] || $campaign->summary !== $catalog['course']['summary']
                || (int)$campaign->category !== (int)$asix->id) {
            return fixture_v3_verification_failure($reason, 'campaign-course-metadata');
        }
        $expectedroles = [];
        $phase = 'identities';
        foreach ($catalog['teachers'] as $identity) {
            $expectedroles[$identity['username']] = 'editingteacher';
        }
        if (!$DB->get_record('user', ['username' => 'student1'])) {
            return fixture_v3_verification_failure($reason, 'student1');
        }
        $expectedroles['student1'] = 'student';
        foreach ($catalog['students'] as $identity) {
            $user = $DB->get_record('user', ['username' => $identity['username']]);
            if (!$user) {
                return fixture_v3_verification_failure($reason, 'student-identity');
            }
            if ($user->auth !== 'nologin' || $user->firstname !== $identity['firstname'] || $user->lastname !== $identity['lastname'] || $user->email !== $identity['email']) {
                return fixture_v3_verification_failure($reason, 'student-metadata');
            }
            $expectedroles[$identity['username']] = 'student';
        }
        foreach ($catalog['teachers'] as $identity) {
            $user = $DB->get_record('user', ['username' => $identity['username']]);
            if (!$user) {
                return fixture_v3_verification_failure($reason, 'teacher-identity');
            }
            if ($user->auth !== 'nologin' || $user->firstname !== $identity['firstname'] || $user->lastname !== $identity['lastname'] || $user->email !== $identity['email']) {
                return fixture_v3_verification_failure($reason, 'teacher-metadata');
            }
        }
        $context = context_course::instance($campaign->id);
        $phase = 'enrolments';
        $enrolled = get_enrolled_users($context, '', 0, 'u.id,u.username');
        if (count($enrolled) !== count($expectedroles)) {
            return fixture_v3_verification_failure($reason, 'enrolment-count');
        }
        foreach ($enrolled as $user) {
            if (!isset($expectedroles[$user->username])) {
                return fixture_v3_verification_failure($reason, 'enrolment-identity');
            }
            $roles = $DB->get_fieldset_sql('SELECT r.shortname FROM {role_assignments} ra JOIN {role} r ON r.id = ra.roleid WHERE ra.contextid = ? AND ra.userid = ?', [$context->id, $user->id]);
            if ($roles !== [$expectedroles[$user->username]]) {
                return fixture_v3_verification_failure($reason, 'enrolment-role');
            }
        }
        $seen = [];
        foreach ($catalog['assignments'] as $index => $spec) {
            $assignment = 'assignment-' . ($index + 1);
            $phase = $assignment;
            $row = $DB->get_record_sql("SELECT a.*, cm.id AS cmid FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module WHERE a.course = ? AND cm.idnumber = ? AND m.name = 'assign'", [$campaign->id, $spec['idnumber']]);
            if (!$row) {
                return fixture_v3_verification_failure($reason, $assignment . '-identity');
            }
            if ($row->name !== $spec['title']) {
                return fixture_v3_verification_failure($reason, $assignment . '-title');
            }
            if ($row->intro !== $spec['intro']) {
                return fixture_v3_verification_failure($reason, $assignment . '-intro-text');
            }
            if ((int)$row->introformat !== (int)FORMAT_HTML) {
                return fixture_v3_verification_failure($reason, $assignment . '-intro-format');
            }
            if ((int)$row->duedate !== fixture_timestamp($anchor, $spec['dueoffset'])) {
                return fixture_v3_verification_failure($reason, $assignment . '-due-date');
            }
            if ((int)$row->allowsubmissionsfromdate !== fixture_timestamp($anchor, $spec['allowoffset'])) {
                return fixture_v3_verification_failure($reason, $assignment . '-allow-date');
            }
            if ($checksubmission && ((int)$row->submissionattachments !== 1 || (int)$row->submissiondrafts !== 0
                || (int)$row->requiresubmissionstatement !== 0
                    || !fixture_v3_submission_file_enabled((int)$row->id))) {
                return fixture_v3_verification_failure($reason, $assignment . '-submission-config');
            }
            $phase = $assignment . '-attachment';
            if (!fixture_v3_verify_files(context_module::instance($row->cmid)->id, $spec['files'])) {
                return fixture_v3_verification_failure($reason, $assignment . '-attachment');
            }
            $seen[$spec['idnumber']] = true;
        }
        $phase = 'campaign-assignment-set';
        return fixture_v3_has_exact_assignments((int)$campaign->id, array_keys($seen))
            || fixture_v3_verification_failure($reason, 'campaign-assignment-set');
    } catch (Throwable) {
        return fixture_v3_verification_failure($reason, 'verification-exception-' . $phase);
    }
}

function fixture_v3_create_user(array $identity): object {
    global $DB;
    $id = user_create_user((object)[
        'username' => $identity['username'], 'firstname' => $identity['firstname'], 'lastname' => $identity['lastname'],
        'email' => $identity['email'], 'auth' => 'nologin', 'confirmed' => 1, 'suspended' => 0,
    ], false, false);
    return $DB->get_record('user', ['id' => $id], '*', MUST_EXIST);
}

function fixture_v3_create_assignment(object $course, array $spec, int $anchor): void {
    $module = (object)[
        'idnumber' => $spec['idnumber'], 'name' => $spec['title'], 'intro' => $spec['intro'],
        'dueoffset' => $spec['dueoffset'], 'allowoffset' => $spec['allowoffset'], 'files' => $spec['files'],
    ];
    create_assignment($course, [
        'idnumber' => $module->idnumber, 'name' => $module->name, 'intro' => $module->intro,
        'dueoffset' => $module->dueoffset, 'allowoffset' => $module->allowoffset, 'files' => $module->files,
        'submissionfile' => true,
    ], $anchor);
}

function fixture_v3_enable_submission_file(int $assignmentid): void {
    global $DB;
    $assignment = $DB->get_record('assign', ['id' => $assignmentid], '*', MUST_EXIST);
    $assignment->submissionattachments = 1;
    $assignment->submissiondrafts = 0;
    $assignment->requiresubmissionstatement = 0;
    $DB->update_record('assign', $assignment);
    foreach ([
        'enabled' => '1', 'maxfilesubmissions' => '1',
        'maxsubmissionsizebytes' => '2097152', 'filetypeslist' => '.md',
    ] as $name => $value) {
        $existing = $DB->get_record('assign_plugin_config', [
            'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'file', 'name' => $name,
        ]);
        if ($existing) {
            $existing->value = $value;
            $DB->update_record('assign_plugin_config', $existing);
        } else {
            $DB->insert_record('assign_plugin_config', (object)[
                'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'file', 'name' => $name, 'value' => $value,
            ]);
        }
    }
    $online = $DB->get_record('assign_plugin_config', [
        'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'onlinetext', 'name' => 'enabled',
    ]);
    if ($online) {
        $online->value = '0';
        $DB->update_record('assign_plugin_config', $online);
    } else {
        $DB->insert_record('assign_plugin_config', (object)[
            'assignment' => $assignmentid, 'subtype' => 'assignsubmission', 'plugin' => 'onlinetext', 'name' => 'enabled', 'value' => '0',
        ]);
    }
}

function expand_fixture(): void {
    global $DB;
    $loaded = fixture_v3_catalog();
    if ($loaded === null) {
        throw new RuntimeException('v3 catalog was not loaded');
    }
    $state = fixture_state();
    if ($state === 'complete-v3') {
        return;
    }
    if ($state === 'complete-v3-submission-config-legacy') {
        $transaction = $DB->start_delegated_transaction();
        try {
            foreach ($loaded['data']['assignments'] as $spec) {
                $assignment = $DB->get_record_sql("SELECT a.* FROM {assign} a JOIN {course_modules} cm ON cm.instance = a.id JOIN {modules} m ON m.id = cm.module WHERE cm.idnumber = ? AND m.name = 'assign'", [$spec['idnumber']], MUST_EXIST);
                fixture_v3_enable_submission_file((int)$assignment->id);
            }
            if (!verify_fixture_v3($loaded['data'], $loaded['digest'], true)) {
                throw new RuntimeException('v3 submission configuration verification failed');
            }
            $transaction->allow_commit();
            return;
        } catch (Throwable $error) {
            $transaction->rollback($error);
        }
    }
    if ($state === 'partial') {
        throw new RuntimeException('rich fixture is partial');
    }
    if ($state === 'absent') {
        seed_fixture();
        $state = fixture_state();
    }
    if ($state === 'complete-v1') {
        advance_fixture();
        $state = fixture_state();
    }
    if ($state !== 'complete-v2') {
        throw new RuntimeException('rich fixture cannot migrate to revision 3');
    }
    $anchor = fixture_anchor(true);
    if ($anchor === null || !verify_fixture(true, $anchor)) {
        throw new RuntimeException('rich fixture v2 verification failed before migration');
    }
    foreach (fixture_v3_usernames() as $username) {
        if ($DB->record_exists('user', ['username' => $username])) {
            throw new RuntimeException('v3 fixture identity collision');
        }
    }
    if ($DB->record_exists('course', ['shortname' => AUTOTASK_FIXTURE_V3_COURSE])) {
        throw new RuntimeException('v3 fixture course collision');
    }
    foreach ($loaded['data']['assignments'] as $spec) {
        if ($DB->record_exists('course_modules', ['idnumber' => $spec['idnumber']])) {
            throw new RuntimeException('v3 fixture assignment collision');
        }
    }
    $transaction = $DB->start_delegated_transaction();
    try {
        if (get_config('core', AUTOTASK_FIXTURE_ANCHOR_CONFIG) === false) {
            set_config(AUTOTASK_FIXTURE_ANCHOR_CONFIG, (string)$anchor);
        }
        $asix = $DB->get_record('course_categories', ['idnumber' => 'AUTOTASK-ASIX'], '*', MUST_EXIST);
        $campaign = create_course((object)['fullname' => $loaded['data']['course']['fullname'], 'shortname' => AUTOTASK_FIXTURE_V3_COURSE, 'category' => $asix->id, 'summary' => $loaded['data']['course']['summary']]);
        $manual = enrol_get_plugin('manual');
        if (!$manual) {
            throw new RuntimeException('manual enrolment plugin unavailable');
        }
        $teacherrole = $DB->get_record('role', ['shortname' => 'editingteacher'], '*', MUST_EXIST);
        $studentrole = $DB->get_record('role', ['shortname' => 'student'], '*', MUST_EXIST);
        $instance = $DB->get_record('enrol', ['courseid' => $campaign->id, 'enrol' => 'manual'], '*', MUST_EXIST);
        foreach ($loaded['data']['teachers'] as $identity) {
            $manual->enrol_user($instance, fixture_v3_create_user($identity)->id, $teacherrole->id);
        }
        $manual->enrol_user($instance, $DB->get_record('user', ['username' => 'student1'], '*', MUST_EXIST)->id, $studentrole->id);
        foreach ($loaded['data']['students'] as $identity) {
            $manual->enrol_user($instance, fixture_v3_create_user($identity)->id, $studentrole->id);
        }
        foreach ($loaded['data']['assignments'] as $spec) {
            fixture_v3_create_assignment($campaign, $spec, $anchor);
        }
        $reason = null;
        if (!verify_fixture_v3($loaded['data'], $loaded['digest'], false, $reason)) {
            throw new RuntimeException('rich fixture verification failed during v3 migration: ' . ($reason ?? 'verification-exception'));
        }
        set_config(AUTOTASK_FIXTURE_CATALOG_DIGEST_CONFIG, $loaded['digest']);
        set_config(AUTOTASK_FIXTURE_CONFIG, '3');
        if (fixture_state() !== 'complete-v3') {
            throw new RuntimeException('rich fixture verification failed after v3 migration');
        }
        $transaction->allow_commit();
    } catch (Throwable $error) {
        $transaction->rollback($error);
    }
}

if (!defined('AUTOTASK_FIXTURE_LIBRARY')) {
    $action = $argv[1] ?? '';
    $catalogpath = $argv[2] ?? '';
    if (in_array($action, ['state', 'ensure', 'seed', 'advance', 'expand'], true)) {
        fixture_v3_load_catalog($catalogpath);
    }
    if ($action === 'state') {
        echo fixture_state();
    } elseif ($action === 'ensure') {
        $state = fixture_state();
        if ($state === 'partial') {
            throw new RuntimeException('rich fixture is partial');
        }
        if ($state === 'absent') {
            seed_fixture();
        } elseif (get_config('core', AUTOTASK_FIXTURE_ANCHOR_CONFIG) === false) {
            $advanced = $state === 'complete-v2' || $state === 'complete-v3';
            $anchor = fixture_anchor($advanced);
            if ($anchor === null || !verify_fixture($advanced, $anchor)) {
                throw new RuntimeException('rich fixture anchor migration failed');
            }
            set_config(AUTOTASK_FIXTURE_ANCHOR_CONFIG, (string)$anchor);
        }
        echo fixture_state();
    } elseif ($action === 'seed') {
        seed_fixture();
        echo 'rich-fixture-seeded';
    } elseif ($action === 'advance') {
        advance_fixture();
        echo 'rich-fixture-advanced';
    } elseif ($action === 'expand') {
        expand_fixture();
        echo 'rich-fixture-expanded';
    } else {
        throw new InvalidArgumentException('unsupported fixture action');
    }
}
