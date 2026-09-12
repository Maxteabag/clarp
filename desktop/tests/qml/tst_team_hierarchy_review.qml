import QtQuick
import QtTest
import "../../qml/components"

TestCase {
 id: testCase
 name: "TeamHierarchyReview"
 width: 900; height: 520; visible: true
 when: windowShown
 QtObject {
  id: stub
  property var teams: [
   {team_id:"root",name:"Research",color:"#718874",member_agent_ids:["atlas"]},
   {team_id:"visual",name:"Visuals",parent_team_id:"root",color:"#9e815e",member_agent_ids:["beacon"]},
   {team_id:"security",name:"Security",parent_team_id:"root",color:"#718874",member_agent_ids:["atlas","beacon"]},
   {team_id:"audit",name:"Review",parent_team_id:"security",color:"#718874",member_agent_ids:["atlas"]}
  ]
  property string selectedTeamId: ""
  property bool teamsLoading: false
  property string teamsError: ""
  property var teamMessages: []
  function teamAgentChoices(){return [{id:"atlas",name:"Atlas"},{id:"beacon",name:"Beacon"}];}
  function agentNameById(id){return id==="atlas"?"Atlas":"Beacon";}
  function selectTeam(id){selectedTeamId=id;}
  function loadTeams(){}
 }
 Component { id: factory; TeamsPanel { controller:stub; width:900;height:520 } }
 function test_capture(){
  const panel=createTemporaryObject(factory,testCase);
  wait(150);
  grabImage(panel).save("/var/tmp/teams-native-hierarchy.png");
 }
}
