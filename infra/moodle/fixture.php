<?php

declare(strict_types=1);

define('CLI_SCRIPT', true);
require '/var/www/html/config.php';
require_once $CFG->dirroot . '/user/lib.php';
require_once $CFG->dirroot . '/course/lib.php';
require_once $CFG->dirroot . '/course/modlib.php';

const AUTOTASK_FIXTURE_CONFIG = 'moddle_autotask_rich_fixture_version';
const AUTOTASK_FIXTURE_ANCHOR_CONFIG = 'moddle_autotask_rich_fixture_anchor';

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
        || $DB->record_exists_select('course_modules', $DB->sql_like('idnumber', '?'), ['autotask-rich-%']);
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
            $expecteddue = $spec['dueoffset'] === 0 ? 0 : $anchor + $spec['dueoffset'];
            if ($spec['idnumber'] === 'autotask-rich-iso-ova' && $advanced) {
                $expecteddue += 86400;
            }
            $expectedallow = $spec['allowoffset'] === 0 ? 0 : $anchor + $spec['allowoffset'];
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
        'alwaysshowdescription' => 1, 'submissionattachments' => 0, 'submissiondrafts' => 0,
        'requiresubmissionstatement' => 0, 'sendnotifications' => 0, 'sendlatenotifications' => 0,
        'sendstudentnotifications' => 0,
        'duedate' => $spec['dueoffset'] === 0 ? 0 : $now + $spec['dueoffset'],
        'allowsubmissionsfromdate' => $spec['allowoffset'] === 0 ? 0 : $now + $spec['allowoffset'],
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

$action = $argv[1] ?? '';
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
        $advanced = $state === 'complete-v2';
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
} else {
    throw new InvalidArgumentException('unsupported fixture action');
}
